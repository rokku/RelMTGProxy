"""FastAPI server for the art-picker UI (spec §6).

Multi-project: a single server instance can list, create, edit, delete, and
export any project under `projects/`. The static UI reads the initial
project from `location.hash` for CLI deep-links.

Endpoints (paths are project-scoped; `{name}` is a project name):

    GET    /api/projects                                → list projects
    POST   /api/projects                                → create from decklist text
    GET    /api/projects/{name}                         → full project JSON
    DELETE /api/projects/{name}                         → delete a project
    GET    /api/projects/{name}/entries/{i}/thumb       → current-selection thumb
    DELETE /api/projects/{name}/entries/{i}             → remove a card
    POST   /api/projects/{name}/select                  → change a card's printing
    POST   /api/projects/{name}/export                  → SSE export progress

    GET    /api/prints/{oracle_id}                      → oracle-level printings
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import urllib.parse
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import archidekt as AK
from . import backs as BK
from . import decklist as DL
from . import moxfield as MX
from . import scryfall as SF
from . import upscale as UP
from .layout import PageSpec
from .pdf_export import (
    DEFAULT_CUT_COLOR, RenderCard, default_output_path,
    extract_pdf_page_bytes, parse_hex_color,
    pdf_page_count, rasterise_pdf_to_pngs, render_pdf,
    render_pdf_page_to_png_bytes, render_registration_test,
)
from .project import Entry, Project, SelectedPrint

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent.parent / "static"
ASSETS_DIR = Path(__file__).parent.parent / "assets"
OUTPUT_DIR = Path(__file__).parent.parent / "output"
UPLOADS_ROOT = Path(__file__).parent.parent / "cache" / "images" / "custom"
BACKS_ROOT = Path(__file__).parent.parent / "cache" / "images" / "backs"

# Shared asset library — one dir, referenced from any project. Underscore
# prefix avoids clashing with a project literally named "library" (the name
# validator refuses leading punctuation, so this is safe).
LIBRARY_DIRNAME = "_library"


def _library_dir() -> Path:
    """Return the current library directory. Resolved at call time so tests
    can monkeypatch `UPLOADS_ROOT` and everything below picks it up."""
    return UPLOADS_ROOT / LIBRARY_DIRNAME

# Reject uploads that aren't recognisable image types up front — protects
# against the user dropping a random ZIP onto the app.
_ALLOWED_UPLOAD_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
_MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # 25 MB per file

# Project names should accept realistic commander names — "Cass, Hand of
# Vengeance", "K'rrik, Son of Yawgmoth", "Mr. House, President and CEO",
# names with diacritics ("Sétya"), etc. Rather than an allowlist regex that
# has to enumerate every possible punctuation mark, we blocklist the small
# set of characters that would be genuinely dangerous or filesystem-hostile.
_FS_HOSTILE = set('/\\<>:"|?*')


class SelectRequest(BaseModel):
    entry_index: int
    scryfall_id: str


class CreateProjectRequest(BaseModel):
    name: str
    decklist: str = ""


class ReorderRequest(BaseModel):
    # New index order: element `i` names the current index of the entry
    # that should end up at position `i` post-reorder.
    order: list[int]


class FromLibraryRequest(BaseModel):
    filenames: list[str]


class SelectLibraryRequest(BaseModel):
    filename: str


class SetDefaultBackRequest(BaseModel):
    filename: str | None = None


class FromScryfallRequest(BaseModel):
    scryfall_id: str
    quantity: int = 1


class QuantityRequest(BaseModel):
    quantity: int


# Bound on how many oracle_ids we hold printings for at once. Each entry is
# up to a few hundred KB of decoded JSON; 512 keeps memory well under 200 MB
# even for a marathon browsing session, without evicting so aggressively
# that the picker feels sluggish on a real deck.
_PRINTINGS_CACHE_MAX = 512


@dataclass
class AppState:
    projects_dir: Path
    client: SF.ScryfallClient
    # Oracle-level printings cache — safe to share across projects because
    # printings only depend on the oracle_id. Bounded LRU semantics via
    # `_touch_printings_cache` so a long-lived server can't drift to
    # unbounded memory.
    printings_cache: OrderedDict[str, list[dict[str, Any]]] = field(
        default_factory=OrderedDict)


def create_app(projects_dir: str | Path = "projects",
               cache_dir: str | Path = "cache") -> FastAPI:
    state = AppState(
        projects_dir=Path(projects_dir),
        client=SF.ScryfallClient(cache_dir=cache_dir),
    )
    state.projects_dir.mkdir(parents=True, exist_ok=True)

    app = FastAPI(title="MTG Proxy Studio")
    app.state.picker = state

    _register_routes(app)
    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    if ASSETS_DIR.exists():
        app.mount("/assets", StaticFiles(directory=ASSETS_DIR), name="assets")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    app.mount("/output", StaticFiles(directory=OUTPUT_DIR), name="output")
    UPLOADS_ROOT.mkdir(parents=True, exist_ok=True)
    app.mount("/uploads", StaticFiles(directory=UPLOADS_ROOT), name="uploads")
    BACKS_ROOT.mkdir(parents=True, exist_ok=True)
    app.mount("/backs", StaticFiles(directory=BACKS_ROOT), name="backs")
    return app


# --- Route table ------------------------------------------------------------

def _register_routes(app: FastAPI) -> None:  # noqa: C901 — single dispatch table
    @app.get("/")
    def index() -> FileResponse:
        idx = STATIC_DIR / "index.html"
        if not idx.exists():
            raise HTTPException(500, f"UI not found at {idx}")
        return FileResponse(idx)

    # --- Project collection ------------------------------------------------
    @app.get("/api/projects")
    def list_projects() -> list[dict[str, Any]]:
        state: AppState = app.state.picker
        out: list[dict[str, Any]] = []
        for p in sorted(state.projects_dir.glob("*.json")):
            try:
                project = Project.load(p.stem, state.projects_dir)
            except Exception as e:
                log.warning("skipping %s: %s", p, e)
                continue
            out.append({
                "name": project.name,
                "entries": len(project.entries),
                "created": project.created,
            })
        return out

    @app.post("/api/projects", status_code=201)
    def create_project(req: CreateProjectRequest) -> dict[str, Any]:
        """Synchronous project creation — used by the CLI and by tests.

        Resolves everything up front and returns the finished project. The
        web UI uses the SSE-streaming variant below for per-card progress.
        """
        state: AppState = app.state.picker
        entries, name = _prepare_new_project(state, req)
        project = Project(name=name)
        failures: list[dict[str, str]] = []
        for de in entries:
            _resolve_and_append(state, project, de, failures)
        project.save(state.projects_dir)
        return {
            "name": name,
            "entries": len(project.entries),
            "failures": failures,
        }

    @app.post("/api/projects/stream")
    async def create_project_stream(req: CreateProjectRequest) -> StreamingResponse:
        """SSE-streaming project creation.

        Emits events so the UI can render a real progress bar:
          - `phase`     — {phase: "moxfield-fetch" | "archidekt-fetch" | "resolving"}
          - `start`     — {total, name}
          - `progress`  — {index, total, name}
          - `card`      — {index, name, status, message?}
          - `done`      — {name, entries, failures}
          - `error`     — {message} or {failures: [[line, text], …]}
        """
        state: AppState = app.state.picker
        return StreamingResponse(_create_stream(state, req),
                                  media_type="text/event-stream")

    # --- Single project ----------------------------------------------------
    @app.get("/api/projects/{name}")
    def get_project(name: str) -> dict[str, Any]:
        state: AppState = app.state.picker
        return _load(name, state).to_dict()

    @app.delete("/api/projects/{name}", status_code=204)
    def delete_project(name: str) -> None:
        import shutil
        state: AppState = app.state.picker
        name = _validate_name(name)
        path = state.projects_dir / f"{name}.json"
        if not path.exists():
            raise HTTPException(404, f"Project {name!r} not found")
        path.unlink()
        # Only clean up per-project upload dirs — the shared library is
        # persistent by design. Older projects that uploaded before the
        # library existed still have per-project dirs; those are safe to
        # remove because nothing outside the project references them.
        legacy_dir = UPLOADS_ROOT / name
        if legacy_dir.exists() and legacy_dir.name != LIBRARY_DIRNAME:
            shutil.rmtree(legacy_dir, ignore_errors=True)

    # --- Entries -----------------------------------------------------------
    @app.get("/api/projects/{name}/entries/{index}/thumb")
    def entry_thumb(name: str, index: int) -> dict[str, Any]:
        state: AppState = app.state.picker
        project = _load(name, state)
        entry = _entry_at(project, index)
        # Custom uploaded art: skip Scryfall entirely, point straight at
        # the /uploads mount. The stored path already carries the correct
        # subdirectory (either `_library/…` or a legacy `{project}/…`),
        # so mirror whatever's in the last two path segments.
        if entry.custom_image_path:
            parts = Path(entry.custom_image_path).parts
            # Last two segments are `<subdir>/<filename>`.
            subdir = parts[-2] if len(parts) >= 2 else project.name
            url = f"/uploads/{subdir}/{parts[-1]}"
            return _entry_view(entry, state, card=None, thumb_url_override=url)
        try:
            card = _find_printing(state, entry.oracle_id, entry.selected_print.scryfall_id)
        except HTTPException:
            card = None
        return _entry_view(entry, state, card=card)

    @app.delete("/api/projects/{name}/entries/{index}", status_code=204)
    def delete_entry(name: str, index: int) -> None:
        state: AppState = app.state.picker
        project = _load(name, state)
        _entry_at(project, index)  # bounds-check
        del project.entries[index]
        project.save(state.projects_dir)

    @app.post("/api/projects/{name}/entries/{index}/quantity")
    def set_entry_quantity(name: str, index: int,
                            req: "QuantityRequest") -> dict[str, Any]:
        """Set an entry's quantity in-place. 1..999."""
        if not 1 <= req.quantity <= 999:
            raise HTTPException(400, "quantity must be between 1 and 999")
        state: AppState = app.state.picker
        project = _load(name, state)
        entry = _entry_at(project, index)
        entry.quantity = req.quantity
        project.save(state.projects_dir)
        return {"ok": True, "entry": _entry_view(entry, state, card=None)}

    @app.post("/api/projects/{name}/entries/{index}/duplicate", status_code=201)
    def duplicate_entry(name: str, index: int) -> dict[str, Any]:
        """Insert a copy of `entries[index]` at `index + 1`.

        The clone preserves everything — selected printing, custom-art path,
        quantity, back mode — so both entries can subsequently diverge (each
        can be art-picked independently). Custom entries share the same
        underlying uploaded file, so there's no filesystem duplication.
        """
        import copy
        state: AppState = app.state.picker
        project = _load(name, state)
        src = _entry_at(project, index)
        project.entries.insert(index + 1, copy.deepcopy(src))
        project.save(state.projects_dir)
        return {"ok": True, "new_index": index + 1,
                "entries": len(project.entries)}

    @app.post("/api/projects/{name}/uploads", status_code=201)
    async def upload_files(name: str,
                            files: list[UploadFile] = File(...)) -> dict[str, Any]:
        """Accept image uploads, save them to the shared library, and add
        entries in this project referencing them.

        As of the library refactor, files live in `cache/images/custom/_library/`
        regardless of which project uploaded them, so the same asset can be
        reused across decks without re-uploading.
        """
        state: AppState = app.state.picker
        project = _load(name, state)
        saved = await _save_uploads_to_library(files)
        added: list[dict[str, Any]] = []
        for info in saved:
            entry_name = _default_entry_name(Path(info["filename"]).stem)
            project.entries.append(Entry(
                quantity=1,
                name=entry_name,
                oracle_id="",
                selected_print=SelectedPrint(scryfall_id="", set="",
                                              collector_number=""),
                layout="normal",
                back="standard",
                custom_image_path=info["path"],
            ))
            added.append({
                "name": entry_name,
                "filename": info["filename"],
                "size": info["size"],
                "path": info["path"],
                "url": info["url"],
            })
        project.save(state.projects_dir)
        return {"added": added, "entries": len(project.entries)}

    @app.get("/api/registration-test")
    def get_registration_test(flip_edge: str = "long",
                                back_offset_x: float = 0.0,
                                back_offset_y: float = 0.0) -> FileResponse:
        """Return the two-page duplex registration PDF as a download.

        Same content as `python cli.py testpage`, but streamable through
        the browser so the user can iterate on the offsets without hitting
        the terminal. Landed in `output/` so it's greppable by timestamp.
        """
        if flip_edge not in ("long", "short"):
            raise HTTPException(400, "flip_edge must be 'long' or 'short'")
        from datetime import datetime
        ts = datetime.now().strftime("%Y-%m-%d-%H%M%S")
        out = OUTPUT_DIR / f"registration_test_{ts}.pdf"
        render_registration_test(
            out,
            flip_edge=flip_edge,     # type: ignore[arg-type]
            back_offset_x_mm=back_offset_x,
            back_offset_y_mm=back_offset_y,
        )
        return FileResponse(out, media_type="application/pdf",
                             filename=out.name)

    # ------------------- Backs library -----------------------------------
    @app.get("/api/backs")
    def list_backs() -> list[dict[str, Any]]:
        BACKS_ROOT.mkdir(parents=True, exist_ok=True)
        out: list[dict[str, Any]] = []
        for p in sorted(BACKS_ROOT.iterdir()):
            if not p.is_file():
                continue
            if p.suffix.lower() not in _ALLOWED_UPLOAD_EXTS:
                continue
            st = p.stat()
            out.append({
                "filename": p.name,
                "url": f"/backs/{p.name}",
                "size": st.st_size,
                "modified": st.st_mtime,
            })
        return out

    @app.post("/api/backs/uploads", status_code=201)
    async def upload_to_backs(files: list[UploadFile] = File(...)) -> dict[str, Any]:
        BACKS_ROOT.mkdir(parents=True, exist_ok=True)
        added: list[dict[str, Any]] = []
        for uf in files:
            ext = Path(uf.filename or "").suffix.lower()
            if ext not in _ALLOWED_UPLOAD_EXTS:
                raise HTTPException(400,
                    f"Unsupported file type {ext!r}. Use PNG, JPG or WebP.")
            safe = _safe_upload_name(uf.filename or f"back{ext}")
            target = _unique_path(BACKS_ROOT / safe)
            size = await _stream_upload_to_disk(uf, target)
            if size == 0:
                target.unlink(missing_ok=True)
                continue
            added.append({
                "filename": target.name,
                "url": f"/backs/{target.name}",
                "size": size,
            })
        return {"added": added,
                "total_in_library": len(list(BACKS_ROOT.iterdir()))}

    @app.delete("/api/backs/{filename}", status_code=204)
    def delete_back(filename: str) -> None:
        safe = _safe_upload_name(filename)
        path = BACKS_ROOT / safe
        if not path.exists():
            raise HTTPException(404, f"back {filename!r} not found")
        path.unlink()

    @app.post("/api/projects/{name}/default-back")
    def set_project_default_back(name: str,
                                   req: SetDefaultBackRequest) -> dict[str, Any]:
        """Set (or clear) the project's default back image.

        Passing `{"filename": null}` clears the setting so exports fall back
        to `assets/mtg_back.png` or the generated placeholder.
        """
        state: AppState = app.state.picker
        project = _load(name, state)
        if req.filename is not None:
            safe = _safe_upload_name(req.filename)
            if "/" in safe or "\\" in safe or safe.startswith("."):
                raise HTTPException(400, "invalid filename")
            if not (BACKS_ROOT / safe).exists():
                raise HTTPException(404, f"back {req.filename!r} not in library")
            project.default_back_filename = safe
        else:
            project.default_back_filename = None
        project.save(state.projects_dir)
        return {"ok": True,
                "default_back_filename": project.default_back_filename}

    # ------------------- Persistent art library --------------------------
    @app.get("/api/library")
    def list_library() -> list[dict[str, Any]]:
        _library_dir().mkdir(parents=True, exist_ok=True)
        out: list[dict[str, Any]] = []
        for p in sorted(_library_dir().iterdir()):
            if not p.is_file():
                continue
            if p.suffix.lower() not in _ALLOWED_UPLOAD_EXTS:
                continue
            st = p.stat()
            out.append({
                "filename": p.name,
                "path": f"cache/images/custom/{LIBRARY_DIRNAME}/{p.name}",
                "url": f"/uploads/{LIBRARY_DIRNAME}/{p.name}",
                "size": st.st_size,
                "modified": st.st_mtime,
            })
        return out

    @app.post("/api/library/uploads", status_code=201)
    async def upload_to_library(files: list[UploadFile] = File(...)) -> dict[str, Any]:
        """Save one or more images to the shared library — does *not* touch
        any project. Use this for pre-loading art you'll add to decks later.
        """
        saved = await _save_uploads_to_library(files)
        return {"added": saved, "total_in_library": len(list(_library_dir().iterdir()))}

    @app.delete("/api/library/{filename}", status_code=204)
    def delete_library_asset(filename: str) -> None:
        safe = _safe_upload_name(filename)
        path = _library_dir() / safe
        if not path.exists():
            raise HTTPException(404, f"library asset {filename!r} not found")
        path.unlink()

    @app.post("/api/projects/{name}/entries/from-library", status_code=201)
    def add_entries_from_library(name: str,
                                  req: FromLibraryRequest) -> dict[str, Any]:
        """Add existing library assets as new entries in a project."""
        state: AppState = app.state.picker
        project = _load(name, state)
        added: list[dict[str, Any]] = []
        missing: list[str] = []
        for raw in req.filenames:
            safe = _safe_upload_name(raw)
            asset = _library_dir() / safe
            if not asset.exists():
                missing.append(raw)
                continue
            entry_name = _default_entry_name(asset.stem)
            rel_path = f"cache/images/custom/{LIBRARY_DIRNAME}/{asset.name}"
            project.entries.append(Entry(
                quantity=1, name=entry_name, oracle_id="",
                selected_print=SelectedPrint(scryfall_id="", set="",
                                              collector_number=""),
                layout="normal", back="standard",
                custom_image_path=rel_path,
            ))
            added.append({"name": entry_name, "filename": asset.name,
                           "path": rel_path,
                           "url": f"/uploads/{LIBRARY_DIRNAME}/{asset.name}"})
        if missing:
            raise HTTPException(404, f"library assets not found: {missing}")
        project.save(state.projects_dir)
        return {"added": added, "entries": len(project.entries)}

    @app.put("/api/projects/{name}/order")
    def reorder_entries(name: str, req: ReorderRequest) -> dict[str, Any]:
        """Rewrite the entries list in the order specified by `req.order`.

        `req.order[i] = j` means "the entry currently at position j should
        end up at position i". Rejected if the list isn't a valid permutation.
        """
        state: AppState = app.state.picker
        project = _load(name, state)
        n = len(project.entries)
        if sorted(req.order) != list(range(n)):
            raise HTTPException(400,
                "order must be a permutation of 0.."
                f"{n - 1} (got {req.order})")
        project.entries = [project.entries[i] for i in req.order]
        project.save(state.projects_dir)
        return {"ok": True, "count": n}

    @app.post("/api/projects/{name}/entries/{index}/select-library")
    def select_library_asset(name: str, index: int,
                              req: SelectLibraryRequest) -> dict[str, Any]:
        """Swap an entry's art to point at a library asset.

        Works whether the entry was previously a Scryfall printing or another
        library asset. Only mutates `custom_image_path`; leaves `selected_print`
        alone so a future "revert to printing" UI can bring the Scryfall art
        back if desired.
        """
        state: AppState = app.state.picker
        project = _load(name, state)
        entry = _entry_at(project, index)
        safe = _safe_upload_name(req.filename)
        asset = _library_dir() / safe
        if not asset.exists():
            raise HTTPException(404, f"library asset {req.filename!r} not found")
        entry.custom_image_path = f"cache/images/custom/{LIBRARY_DIRNAME}/{asset.name}"
        project.save(state.projects_dir)
        thumb_url = f"/uploads/{LIBRARY_DIRNAME}/{asset.name}"
        return {"ok": True, "entry": _entry_view(entry, state, card=None,
                                                   thumb_url_override=thumb_url)}

    @app.get("/api/scryfall/search")
    def scryfall_search(q: str, kind: str = "card",
                        limit: int = 30) -> dict[str, Any]:
        """Search Scryfall's card database for cards or tokens.

        `kind` narrows to normal cards (`card`, excludes tokens) or tokens
        (`token`, adds `t:token`). Returns at most `limit` thumbnail rows.
        """
        state: AppState = app.state.picker
        term = (q or "").strip()
        if not term:
            return {"results": [], "total_cards": 0}
        if kind not in ("card", "token"):
            raise HTTPException(400, "kind must be 'card' or 'token'")
        limit = max(1, min(60, limit))
        # `unique=cards` collapses art variants — one row per named card,
        # which is what we want for an "add this card to the deck" search.
        # The token filter keeps only tokens; the card filter excludes them.
        modifier = "t:token" if kind == "token" else "-t:token"
        query = f"{modifier} {term}".strip()
        url = (f"{SF.API_BASE}/cards/search"
               f"?q={urllib.parse.quote(query)}&unique=cards&order=released")
        data = state.client._get_json(url)
        if data.get("__http_status") == 404:
            return {"results": [], "total_cards": 0}
        rows = list(data.get("data", []))[:limit]
        return {
            "results": [_thumbnail_view(p) for p in rows],
            "total_cards": data.get("total_cards", len(rows)),
        }

    @app.post("/api/projects/{name}/entries/from-scryfall", status_code=201)
    def add_entry_from_scryfall(name: str,
                                 req: FromScryfallRequest) -> dict[str, Any]:
        """Add a new deck entry by Scryfall card id."""
        state: AppState = app.state.picker
        project = _load(name, state)
        if req.quantity < 1:
            raise HTTPException(400, "quantity must be >= 1")
        card = state.client._get_json(f"{SF.API_BASE}/cards/{req.scryfall_id}")
        if card.get("__http_status") == 404:
            raise HTTPException(404,
                f"Card {req.scryfall_id!r} not found on Scryfall")
        entry = Entry(
            quantity=req.quantity,
            name=card.get("name", ""),
            oracle_id=card.get("oracle_id", ""),
            selected_print=SelectedPrint(
                scryfall_id=card["id"],
                set=card.get("set", ""),
                collector_number=card.get("collector_number", ""),
            ),
            layout=card.get("layout", "normal"),
            back="face" if card.get("layout") in SF.DFC_LAYOUTS else "standard",
        )
        project.entries.append(entry)
        project.save(state.projects_dir)
        return {
            "ok": True,
            "index": len(project.entries) - 1,
            "entry": _entry_view(entry, state, card=card),
        }

    @app.post("/api/projects/{name}/select")
    def select_printing(name: str, req: SelectRequest) -> dict[str, Any]:
        state: AppState = app.state.picker
        project = _load(name, state)
        entry = _entry_at(project, req.entry_index)
        card = _find_printing(state, entry.oracle_id, req.scryfall_id)
        entry.selected_print = SelectedPrint(
            scryfall_id=card["id"],
            set=card.get("set", ""),
            collector_number=card.get("collector_number", ""),
        )
        entry.layout = card.get("layout", entry.layout)
        entry.back = "face" if card.get("layout") in SF.DFC_LAYOUTS else "standard"
        # Picking a Scryfall printing is a clear "use this art" signal —
        # drop any prior custom-image override so the choice takes effect
        # (previously the entry kept both fields and every consumer
        # short-circuited on the custom path).
        entry.custom_image_path = None
        # If the entry started life as a custom-art upload, it had no
        # oracle_id — capture the Scryfall one now so the Library <->
        # Printings tabs stay consistent on the next open.
        if not entry.oracle_id:
            entry.oracle_id = card.get("oracle_id", "")
        project.save(state.projects_dir)
        return {"ok": True, "entry": _entry_view(entry, state, card=card)}

    # --- Upscaler comparison test ------------------------------------------
    @app.post("/api/upscale-test/stream")
    def upscale_test_stream(scryfall_id: str) -> StreamingResponse:
        """Stream a side-by-side upscaler comparison for one card.

        Runs every model in `UP.MODELS` once at the model's native 4× scale,
        saves the 1200 DPI result, then downsamples to 600 DPI with LANCZOS
        and saves that too. Six PNGs total (3 models × 2 scales), streamed
        as SSE progress + a final `done` event with URLs.
        """
        state: AppState = app.state.picker
        if not scryfall_id.strip():
            raise HTTPException(400, "scryfall_id is required")
        return StreamingResponse(
            _upscale_test_stream(state, scryfall_id.strip()),
            media_type="text/event-stream",
        )

    # --- Prints ------------------------------------------------------------
    @app.get("/api/prints/{oracle_id}")
    def get_prints(oracle_id: str) -> dict[str, Any]:
        state: AppState = app.state.picker
        if oracle_id in state.printings_cache:
            printings = state.printings_cache[oracle_id]
            state.printings_cache.move_to_end(oracle_id)  # LRU touch
        else:
            url = (f"{SF.API_BASE}/cards/search?q=oracleid%3A{oracle_id}"
                   f"&unique=prints&order=released")
            first = state.client._get_json(url)
            if first.get("__http_status") == 404:
                raise HTTPException(404, f"No printings for oracle_id {oracle_id!r}")
            printings = list(first.get("data", []))
            next_url = first.get("next_page") if first.get("has_more") else None
            while next_url:
                page = state.client._get_json(next_url)
                printings.extend(page.get("data", []))
                next_url = page.get("next_page") if page.get("has_more") else None
            state.printings_cache[oracle_id] = printings
            # Evict oldest entries once the cap is exceeded.
            while len(state.printings_cache) > _PRINTINGS_CACHE_MAX:
                state.printings_cache.popitem(last=False)
        return {
            "oracle_id": oracle_id,
            "printings": [_thumbnail_view(p) for p in printings],
        }

    # --- Export ------------------------------------------------------------
    @app.post("/api/projects/{name}/export")
    def post_export(name: str,
                    gutter: float = 3.0,
                    cut_lines: str = "full",
                    cut_color: str = "#4d8bff",
                    upscale: bool | None = None,
                    quality: str = "quality",
                    backs: str = "none",
                    flip_edge: str = "long",
                    back_offset_x: float = 0.0,
                    back_offset_y: float = 0.0,
                    format: str = "pdf",
                    png_dpi: int = 300,
                    paper: str = "A4",
                    dpi_target: int = 600,
                    bleed: float = 0.0) -> StreamingResponse:
        if cut_lines not in ("full", "ticks"):
            raise HTTPException(400, "cut_lines must be 'full' or 'ticks'")
        if backs not in ("none", "duplex", "separate"):
            raise HTTPException(400, "backs must be 'none', 'duplex' or 'separate'")
        if flip_edge not in ("long", "short"):
            raise HTTPException(400, "flip_edge must be 'long' or 'short'")
        if quality not in UP.MODELS:
            raise HTTPException(400,
                f"quality must be one of {list(UP.MODELS)}")
        if format not in ("pdf", "png"):
            raise HTTPException(400, "format must be 'pdf' or 'png'")
        if not 72 <= png_dpi <= 1200:
            raise HTTPException(400, "png_dpi must be between 72 and 1200")
        if dpi_target not in (600, 1200):
            raise HTTPException(400, "dpi_target must be 600 or 1200")
        if not 0.0 <= bleed <= 10.0:
            raise HTTPException(400, "bleed must be between 0 and 10 mm")
        from .layout import PAPERS_MM
        if paper not in PAPERS_MM:
            raise HTTPException(400,
                f"paper must be one of {list(PAPERS_MM)}")
        try:
            color = parse_hex_color(cut_color)
        except ValueError as e:
            raise HTTPException(400, f"cut_color: {e}") from e
        state: AppState = app.state.picker
        project_name = _validate_name(name)
        # Bleed needs room to extend into the gutter without overlapping
        # the neighbouring card's bleed. Auto-widen rather than error out
        # so users can turn bleed on without also having to remember the
        # gutter constraint.
        effective_gutter = max(gutter, 2 * bleed) if bleed > 0 else gutter
        spec = PageSpec(paper=paper, gutter_mm=effective_gutter,
                         cut_line_mode=cut_lines,
                         bleed_mm=bleed)  # type: ignore[arg-type]
        want_upscale = UP.any_backend_installed() if upscale is None else upscale
        return StreamingResponse(
            _export_stream(state, project_name=project_name, spec=spec,
                           cut_color=color, upscale_on=want_upscale,
                           quality=quality,
                           backs_mode=backs,
                           flip_edge=flip_edge,  # type: ignore[arg-type]
                           back_offset=(back_offset_x, back_offset_y),
                           output_format=format,
                           png_dpi=png_dpi,
                           dpi_target=dpi_target),
            media_type="text/event-stream",
        )

    # --- Post-export preview + per-page download ---------------------------
    @app.get("/api/pdf-preview")
    def pdf_preview(path: str, page: int = 1, dpi: int = 90) -> "Response":
        """Rasterise one page of a produced PDF to a PNG.

        `path` must resolve inside `output/` — no arbitrary filesystem
        access. `dpi` is capped at 200 to keep preview payloads small.
        """
        from fastapi import Response
        if not 40 <= dpi <= 200:
            raise HTTPException(400, "dpi must be between 40 and 200")
        pdf_path = _resolve_output_path(path)
        try:
            data = render_pdf_page_to_png_bytes(pdf_path, page - 1, dpi=dpi)
        except IndexError as e:
            raise HTTPException(404, str(e)) from e
        # Preview PNGs are safe to cache aggressively — the source PDF's
        # filename is timestamped, so a URL uniquely identifies its content.
        return Response(
            content=data, media_type="image/png",
            headers={"Cache-Control": "public, max-age=86400, immutable"},
        )

    @app.get("/api/pdf-page")
    def pdf_page_download(path: str, page: int = 1) -> "Response":
        """Return a single-page PDF split from a produced export.

        Useful for "reprint just page 3 after a paper jam" — extracts the
        chosen page in-memory and streams it as a download, no server-side
        artefacts left behind.
        """
        from fastapi import Response
        pdf_path = _resolve_output_path(path)
        try:
            data = extract_pdf_page_bytes(pdf_path, page - 1)
        except IndexError as e:
            raise HTTPException(404, str(e)) from e
        stem = pdf_path.stem
        filename = f"{stem}_p{page:02d}.pdf"
        return Response(
            content=data, media_type="application/pdf",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Cache-Control": "public, max-age=3600",
            },
        )

    # --- Upscaler model manager -------------------------------------------
    @app.get("/api/upscaler/status")
    def upscaler_status() -> dict[str, Any]:
        """Report install state for every model, per backend.

        Community models (Ultramix) only have an ncnn entry; base-bundle
        models (x4plus, x4plus-anime) can't be installed piecemeal — the
        `installable` flag is False for those.
        """
        models = []
        for name, spec in UP.MODELS.items():
            ncnn_installed = UP.ncnn_model_installed(quality=name)
            mps_installed = UP.mps_weights_installed(quality=name)
            models.append({
                "quality": name,
                "ncnn_name": spec.ncnn_name,
                "description": spec.description,
                "ncnn": {
                    "installed": ncnn_installed,
                    "installable": bool(spec.ncnn_bin_url),
                    "size_url": spec.ncnn_bin_url,
                },
                "mps": {
                    "installed": mps_installed,
                    "installable": bool(spec.mps_weights_url),
                },
            })
        return {
            "models": models,
            "ncnn_binary_installed": UP.is_binary_installed(),
            "torch_mps_available": UP.torch_mps_available(),
        }

    @app.post("/api/upscaler/install/{quality}")
    def upscaler_install(quality: str, backend: str = "ncnn") -> dict[str, Any]:
        """Download the model files for `quality` on the given backend.

        Runs synchronously — the ncnn `.bin` files are small (~65 MB) and
        the MPS `.pth` files ~65 MB, so a plain POST is simpler than SSE.
        The user's browser shows its own spinner.
        """
        if quality not in UP.MODELS:
            raise HTTPException(404, f"unknown quality {quality!r}")
        if backend not in ("ncnn", "mps"):
            raise HTTPException(400, "backend must be 'ncnn' or 'mps'")
        try:
            if backend == "ncnn":
                if not UP.is_binary_installed():
                    raise HTTPException(400,
                        "ncnn base binary not installed. Run "
                        "`python cli.py setup-upscaler` first.")
                bin_path, _ = UP.download_ncnn_model(quality)
                return {"ok": True, "backend": "ncnn",
                        "installed_at": str(bin_path.parent)}
            weights = UP.download_mps_weights(quality)
            return {"ok": True, "backend": "mps",
                    "installed_at": str(weights)}
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        except Exception as e:
            log.exception("upscaler install failed")
            raise HTTPException(500, f"install failed: {e}") from e

    @app.delete("/api/upscaler/install/{quality}", status_code=200)
    def upscaler_uninstall(quality: str, backend: str = "ncnn") -> dict[str, Any]:
        if quality not in UP.MODELS:
            raise HTTPException(404, f"unknown quality {quality!r}")
        if backend not in ("ncnn", "mps"):
            raise HTTPException(400, "backend must be 'ncnn' or 'mps'")
        try:
            if backend == "ncnn":
                removed = UP.uninstall_ncnn_model(quality)
                return {"ok": True, "removed": [str(p) for p in removed]}
            removed_path = UP.uninstall_mps_weights(quality)
            return {"ok": True,
                    "removed": [str(removed_path)] if removed_path else []}
        except ValueError as e:
            raise HTTPException(400, str(e)) from e


# --- Helpers ----------------------------------------------------------------

# Only these characters survive the project-slug pass; everything else
# collapses to `_`. Keeps the slug filesystem-safe on every OS (no colons,
# no separators, no reserved-name shenanigans on Windows either since the
# slug ends up as a *folder* under `output/`, not a top-level device name).
_SLUG_KEEP = re.compile(r"[a-z0-9]+")


def _slugify_project_name(name: str) -> str:
    """Return a lowercase, underscore-joined slug for use as a folder name.

    Examples:
      "Mazirek Sacrifice"      -> "mazirek_sacrifice"
      "Cass, Hand of Vengeance"-> "cass_hand_of_vengeance"
      "K'rrik, Son of Yawgmoth"-> "k_rrik_son_of_yawgmoth"

    Non-alphanumeric characters collapse to underscore. If the name is
    entirely non-alphanumeric (very rare, but e.g. "…"), falls back to
    "project" so we don't return an empty folder name.
    """
    tokens = _SLUG_KEEP.findall((name or "").lower())
    return "_".join(tokens) if tokens else "project"


def _project_output_dir(project_name: str) -> Path:
    """`output/{slug}/` for `project_name`, created if missing."""
    d = OUTPUT_DIR / _slugify_project_name(project_name)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _resolve_output_path(rel_path: str) -> Path:
    """Resolve `rel_path` under `output/`, rejecting anything that escapes it.

    Accepts both `foo.pdf` and `output/foo.pdf` (the export `done` event returns
    the latter, so this keeps client code trivial).
    """
    if not rel_path:
        raise HTTPException(400, "path is required")
    root = OUTPUT_DIR.resolve()
    cleaned = rel_path.lstrip("/")
    if cleaned.startswith("output/"):
        cleaned = cleaned[len("output/"):]
    candidate = (root / cleaned).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as e:
        raise HTTPException(400, "path must be under output/") from e
    if not candidate.exists() or not candidate.is_file():
        raise HTTPException(404, f"file not found: {rel_path}")
    if candidate.suffix.lower() != ".pdf":
        raise HTTPException(400, "path must point to a PDF")
    return candidate


async def _stream_upload_to_disk(uf: "UploadFile", target: Path,
                                  max_bytes: int = _MAX_UPLOAD_BYTES,
                                  chunk_size: int = 1024 * 1024) -> int:
    """Copy `uf` to `target` in chunks; abort if `max_bytes` is exceeded.

    Returns the total bytes written. Deletes the partial file on abort so
    a rejected upload doesn't leave debris. Never holds more than one
    chunk in memory, so a malicious client can't OOM the process by
    streaming multiple gigabytes at a single-file upload endpoint.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    try:
        with target.open("wb") as fh:
            while True:
                chunk = await uf.read(chunk_size)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    fh.close()
                    target.unlink(missing_ok=True)
                    raise HTTPException(400,
                        f"{uf.filename}: file too large "
                        f"(>{max_bytes // 1024 // 1024} MB)")
                fh.write(chunk)
    except HTTPException:
        raise
    except Exception:
        target.unlink(missing_ok=True)
        raise
    return total


async def _save_uploads_to_library(files: "list[UploadFile]") -> list[dict[str, Any]]:
    """Write each uploaded image to the shared library dir.

    Returns per-file metadata: {filename, path, url, size}. Files whose
    (sanitised) filename collides with an existing library asset get a
    numeric suffix (`sol_ring-2.png`) rather than overwriting.
    """
    _library_dir().mkdir(parents=True, exist_ok=True)
    out: list[dict[str, Any]] = []
    for uf in files:
        ext = Path(uf.filename or "").suffix.lower()
        if ext not in _ALLOWED_UPLOAD_EXTS:
            raise HTTPException(400,
                f"Unsupported file type {ext!r}. Use PNG, JPG or WebP.")
        safe = _safe_upload_name(uf.filename or f"upload{ext}")
        target = _unique_path(_library_dir() / safe)
        size = await _stream_upload_to_disk(uf, target)
        if size == 0:
            target.unlink(missing_ok=True)
            continue
        out.append({
            "filename": target.name,
            "path": f"cache/images/custom/{LIBRARY_DIRNAME}/{target.name}",
            "url": f"/uploads/{LIBRARY_DIRNAME}/{target.name}",
            "size": size,
        })
    return out


def _prepare_new_project(state: "AppState",
                          req: "CreateProjectRequest",
                          ) -> "tuple[list[DL.DeckEntry], str]":
    """Turn a create-project request into a validated (entries, name) pair.

    Handles the three input modes (Moxfield URL, decklist text, empty),
    raises HTTPException with a useful body on any failure. Shared between
    the sync and streaming endpoints.
    """
    decklist_stripped = (req.decklist or "").strip()
    entries: list[DL.DeckEntry] = []
    # Archidekt first — see cli.py for the reason (bare numeric IDs).
    if AK.looks_like_archidekt(decklist_stripped):
        try:
            ak_name, entries = AK.fetch_deck(decklist_stripped)
        except AK.ArchidektError as e:
            raise HTTPException(400, f"Archidekt import failed: {e}") from e
        raw_name = (req.name or "").strip() or AK.sanitize_project_name(ak_name)
    elif MX.looks_like_moxfield(decklist_stripped):
        try:
            mox_name, entries = MX.fetch_deck(decklist_stripped)
        except MX.MoxfieldError as e:
            raise HTTPException(400, f"Moxfield import failed: {e}") from e
        raw_name = (req.name or "").strip() or MX.sanitize_project_name(mox_name)
    elif decklist_stripped:
        try:
            entries = DL.parse_text(decklist_stripped)
        except DL.DecklistError as e:
            raise HTTPException(400, {"error": "decklist parse failed",
                                       "failures": e.failures}) from e
        raw_name = req.name
    else:
        raw_name = req.name

    name = _validate_name(raw_name)
    target = state.projects_dir / f"{name}.json"
    if target.exists():
        raise HTTPException(409, f"Project {name!r} already exists")
    return entries, name


def _resolve_and_append(state: "AppState",
                        project: "Project",
                        de: "DL.DeckEntry",
                        failures: list[dict[str, str]]) -> None:
    """Resolve one decklist entry on Scryfall and append it to the project.

    Failures are captured in `failures` rather than raised, so batch
    creation always finishes with a saveable project (even if a couple
    of cards couldn't be resolved).
    """
    try:
        card = state.client.resolve_named(de.name, de.set_code, de.collector_number)
    except SF.NotFoundError as e:
        failures.append({"name": de.name, "message": str(e)})
        return
    except SF.ScryfallError as e:
        failures.append({"name": de.name, "message": str(e)})
        return
    project.add_entry(Entry(
        quantity=de.quantity,
        name=card.get("name", de.name),
        oracle_id=card.get("oracle_id", ""),
        selected_print=SelectedPrint(
            scryfall_id=card["id"],
            set=card.get("set", ""),
            collector_number=card.get("collector_number", ""),
        ),
        layout=card.get("layout", "normal"),
        back="face" if card.get("layout") in SF.DFC_LAYOUTS else "standard",
    ))


async def _create_stream(state: "AppState",
                          req: "CreateProjectRequest") -> AsyncIterator[bytes]:
    """SSE generator for project creation with per-card progress."""

    def _sse(event: str, data: dict[str, Any]) -> bytes:
        return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()

    # --- Parse input + validate project name (fast, sync). --------------
    decklist_stripped = (req.decklist or "").strip()
    if AK.looks_like_archidekt(decklist_stripped):
        yield _sse("phase", {"phase": "archidekt-fetch"})
        try:
            ak_name, entries = await asyncio.to_thread(
                AK.fetch_deck, decklist_stripped)
        except AK.ArchidektError as e:
            yield _sse("error", {"message": f"Archidekt import failed: {e}"})
            return
        raw_name = (req.name or "").strip() or AK.sanitize_project_name(ak_name)
    elif MX.looks_like_moxfield(decklist_stripped):
        yield _sse("phase", {"phase": "moxfield-fetch"})
        try:
            mox_name, entries = await asyncio.to_thread(
                MX.fetch_deck, decklist_stripped)
        except MX.MoxfieldError as e:
            yield _sse("error", {"message": f"Moxfield import failed: {e}"})
            return
        raw_name = (req.name or "").strip() or MX.sanitize_project_name(mox_name)
    elif decklist_stripped:
        try:
            entries = DL.parse_text(decklist_stripped)
        except DL.DecklistError as e:
            yield _sse("error", {"failures": e.failures,
                                  "message": "Decklist parse failed"})
            return
        raw_name = req.name
    else:
        entries = []
        raw_name = req.name

    try:
        name = _validate_name(raw_name)
    except HTTPException as e:
        yield _sse("error", {"message": _http_detail_to_str(e.detail)})
        return
    target = state.projects_dir / f"{name}.json"
    if target.exists():
        yield _sse("error", {"message": f"Project {name!r} already exists"})
        return

    # --- Resolve each card on Scryfall, streaming progress. --------------
    total = len(entries)
    yield _sse("start", {"name": name, "total": total})
    yield _sse("phase", {"phase": "resolving"})

    project = Project(name=name)
    failures: list[dict[str, str]] = []
    for idx, de in enumerate(entries):
        yield _sse("progress", {"index": idx, "total": total, "name": de.name})
        # Do the Scryfall call off-thread so the event loop stays responsive.
        try:
            await asyncio.to_thread(_resolve_and_append,
                                     state, project, de, failures)
        except Exception as e:
            failures.append({"name": de.name, "message": str(e)})

    project.save(state.projects_dir)
    yield _sse("done", {
        "name": name,
        "entries": len(project.entries),
        "failures": failures,
    })


def _http_detail_to_str(detail: Any) -> str:
    if isinstance(detail, str):
        return detail
    return json.dumps(detail)


# Windows reserves these names at the OS level — any file called `CON`,
# `CON.png`, `nul.jpg`, etc. fails at open() time with an OSError. Pattern
# matches the stem (before the first dot), case-insensitively.
_WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def _safe_upload_name(filename: str) -> str:
    """Sanitise an uploaded file's name to keep it filesystem-safe.

    Guarantees on the returned string (callers rely on these):
      - no path separators (`/`, `\\`) — replaced with `_`
      - no Windows-hostile chars (`<>:"|?*`) or control chars
      - never starts with `.` (leading dots/spaces stripped)
      - never a Windows-reserved device name (`CON.png` → `_CON.png`)
      - always non-empty; falls back to `"upload"`

    So `BACKS_ROOT / _safe_upload_name(x)` and equivalents cannot escape
    the intended directory even if `x` was adversarial.
    """
    hostile = set('/\\<>:"|?*')
    cleaned = "".join(("_" if (c in hostile or ord(c) < 0x20) else c)
                      for c in filename)
    cleaned = cleaned.strip(" .")
    if not cleaned:
        return "upload"
    # Reserved-name check operates on the pre-extension stem.
    stem, _, _ = cleaned.partition(".")
    if stem.upper() in _WINDOWS_RESERVED_NAMES:
        cleaned = "_" + cleaned
    return cleaned


def _unique_path(target: Path) -> Path:
    """If `target` already exists, append `-2`, `-3`, ... to the stem."""
    if not target.exists():
        return target
    stem, suffix = target.stem, target.suffix
    for i in range(2, 1000):
        candidate = target.with_name(f"{stem}-{i}{suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"Could not find a free filename around {target}")


def _default_entry_name(stem: str) -> str:
    """Turn a filename stem into a nicer default entry name."""
    words = stem.replace("_", " ").replace("-", " ").split()
    if not words:
        return "Custom card"
    return " ".join(w[:1].upper() + w[1:] for w in words)


def _validate_name(name: str) -> str:
    s = (name or "").strip()
    if not s:
        raise HTTPException(400, "Project name cannot be empty.")
    if len(s) > 64:
        raise HTTPException(400, "Project name must be 64 characters or fewer.")
    if not s[0].isalnum():
        # Blocks leading `.` (hidden files), `-` (argparse-lookalike), and
        # any other punctuation. Letters and digits are the only reasonable
        # first character.
        raise HTTPException(400,
            "Project name must start with a letter or digit.")
    if ".." in s:
        raise HTTPException(400, "Project name cannot contain '..'.")
    bad = _FS_HOSTILE & set(s)
    if bad:
        listed = " ".join(sorted(bad))
        raise HTTPException(400,
            f"Project name cannot contain: {listed}")
    if any(ord(c) < 0x20 for c in s):
        raise HTTPException(400, "Project name cannot contain control characters.")
    return s


def _load(name: str, state: AppState) -> Project:
    name = _validate_name(name)
    try:
        return Project.load(name, state.projects_dir)
    except FileNotFoundError as e:
        raise HTTPException(404, f"Project {name!r} not found") from e


def _entry_at(project: Project, index: int) -> Entry:
    if not (0 <= index < len(project.entries)):
        raise HTTPException(400, f"entry index {index} out of range for "
                                  f"project with {len(project.entries)} entries")
    return project.entries[index]


def _thumbnail_view(p: dict[str, Any]) -> dict[str, Any]:
    layout = p.get("layout", "normal")
    front_url = _thumb_url(p, face_index=0)
    back_url = _thumb_url(p, face_index=1) if layout in SF.DFC_LAYOUTS else None
    return {
        "id": p["id"],
        "name": p.get("name", ""),
        "set": p.get("set", ""),
        "set_name": p.get("set_name", ""),
        "set_type": p.get("set_type", ""),
        "collector_number": p.get("collector_number", ""),
        "released_at": p.get("released_at", ""),
        "digital": bool(p.get("digital", False)),
        "lang": p.get("lang", ""),
        "frame": p.get("frame", ""),
        "frame_effects": p.get("frame_effects", []),
        "border_color": p.get("border_color", ""),
        "layout": layout,
        "image_url": front_url,
        "back_image_url": back_url,
        "image_status": p.get("image_status", ""),
    }


def _thumb_url(p: dict[str, Any], face_index: int = 0) -> str | None:
    imgs = p.get("image_uris")
    if not imgs:
        faces = p.get("card_faces", [])
        if len(faces) > face_index:
            imgs = faces[face_index].get("image_uris")
    if not imgs:
        return None
    return imgs.get("normal") or imgs.get("large") or imgs.get("small")


def _entry_view(entry: Entry, state: AppState, *,
                card: dict[str, Any] | None = None,
                thumb_url_override: str | None = None) -> dict[str, Any]:
    thumb_url = thumb_url_override or (_thumb_url(card) if card else None)
    return {
        "name": entry.name,
        "quantity": entry.quantity,
        "oracle_id": entry.oracle_id,
        "layout": entry.layout,
        "back": entry.back,
        "selected_print": {
            "scryfall_id": entry.selected_print.scryfall_id,
            "set": entry.selected_print.set,
            "collector_number": entry.selected_print.collector_number,
        },
        "thumb_url": thumb_url,
        "custom_image_path": entry.custom_image_path,
    }


def _find_printing(state: AppState, oracle_id: str,
                   scryfall_id: str) -> dict[str, Any]:
    printings = state.printings_cache.get(oracle_id)
    if printings:
        for p in printings:
            if p["id"] == scryfall_id:
                return p
    url = f"{SF.API_BASE}/cards/{scryfall_id}"
    data = state.client._get_json(url)
    if data.get("__http_status") == 404:
        raise HTTPException(404, f"Card {scryfall_id!r} not found on Scryfall")
    return data


# --- Export SSE stream ------------------------------------------------------

# Heartbeat interval for long-running SSE work. Browsers and intermediaries
# can time out an idle response body after ~30 s; upscaling one card can take
# longer than that, so we emit an SSE comment every few seconds while the
# blocking work runs in a thread.
_HEARTBEAT_INTERVAL_S = 5.0


def _dpi_gate_kw(image_path, label):
    """DPI gate as a positional-args function so `asyncio.to_thread` can call it."""
    return UP.check_dpi_gate(image_path, label=label)


def _resolve_back(entry, client, upscaler, library_filename=None, scale=2):
    return BK.resolve_back_image(
        entry, client=client, upscaler=upscaler,
        scale=scale,
        library_filename=library_filename,
        library_dir=BACKS_ROOT,
    )


async def _run_with_heartbeats(fn, *args):
    """Run `fn(*args)` in a thread; yield SSE keep-alive bytes while waiting.

    Returns an async generator: each yielded value is a `bytes` chunk to
    forward to the client, except for the final yielded item which is the
    result wrapped in a one-tuple sentinel `(_Result, value)`.
    Callers consume with `async for item in _run_with_heartbeats(...)`.
    """
    task = asyncio.ensure_future(asyncio.to_thread(fn, *args))
    async for item in _run_with_heartbeats_task(task):
        yield item


async def _run_with_heartbeats_task(task: "asyncio.Task"):
    """Same shape as `_run_with_heartbeats`, but takes an already-scheduled
    task (used by the download-prefetch pipeline where the task was created
    on a previous iteration)."""
    while not task.done():
        try:
            await asyncio.wait_for(asyncio.shield(task),
                                    timeout=_HEARTBEAT_INTERVAL_S)
            break
        except asyncio.TimeoutError:
            yield b": ping\n\n"
    yield ("__result__", task.result())


def _upscale_kw(src, scryfall_id, face_index, quality, scale):
    """`asyncio.to_thread` only forwards positional args, so wrap the
    upscale call so we can thread the `quality`/`scale` kwargs through."""
    return UP.upscale_image(src, scryfall_id, face_index,
                            quality=quality, scale=scale)


UPSCALE_TEST_DIR = OUTPUT_DIR / "upscale-tests"


def _upscale_native_4x(src: Path, out: Path, quality: str) -> Path:
    """Run one model at its native 4× scale; PNG saved at `out`.

    Same pipeline as the export path, minus the LANCZOS downsample — so
    the caller can decide whether to keep the raw 4× or downsample.
    """
    upscaler = UP.select_upscaler("auto", quality=quality)
    upscaler.upscale(src, out, scale=4)
    return out


def _downsample_to_2x(src_4x: Path, out: Path, src_orig: Path) -> Path:
    """Take a 4× PNG and LANCZOS-downsample to 2× of the original size."""
    from PIL import Image
    with Image.open(src_orig) as orig:
        target = (orig.width * 2, orig.height * 2)
    with Image.open(src_4x) as im:
        icc = im.info.get("icc_profile")
        im = im.resize(target, Image.LANCZOS)
        params: dict[str, object] = {"format": "PNG", "optimize": False}
        if icc is not None:
            params["icc_profile"] = icc
        im.save(out, **params)
    return out


async def _upscale_test_stream(state: AppState,
                                scryfall_id: str) -> AsyncIterator[bytes]:
    """SSE generator for the single-card upscaler comparison test."""

    def _sse(event: str, data: dict[str, Any]) -> bytes:
        return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()

    # Fetch card metadata + download the front image.
    try:
        card = await asyncio.to_thread(
            state.client._get_json, f"{SF.API_BASE}/cards/{scryfall_id}")
    except SF.ScryfallError as e:
        yield _sse("error", {"message": str(e)})
        return
    if card.get("__http_status") == 404:
        yield _sse("error", {"message": f"Card {scryfall_id!r} not found"})
        return

    try:
        front, _back = state.client.face_images_for(card)
    except SF.ScryfallError as e:
        yield _sse("error", {"message": str(e)})
        return

    yield _sse("start", {
        "scryfall_id": scryfall_id,
        "name": card.get("name", ""),
        "set": card.get("set", ""),
        "collector_number": card.get("collector_number", ""),
        "models": list(UP.MODELS),
    })

    yield _sse("progress", {"phase": "download",
                             "name": card.get("name", ""),
                             "index": 0, "total": len(UP.MODELS)})
    try:
        src_path = await asyncio.to_thread(state.client.download_image, front)
    except SF.ScryfallError as e:
        yield _sse("error", {"message": f"download failed: {e}"})
        return

    out_dir = UPSCALE_TEST_DIR / scryfall_id
    out_dir.mkdir(parents=True, exist_ok=True)

    results: list[dict[str, Any]] = []
    for idx, quality in enumerate(UP.MODELS):
        model_spec = UP.MODELS[quality]
        yield _sse("progress", {
            "phase": "upscale",
            "quality": quality,
            "model": model_spec.ncnn_name,
            "index": idx, "total": len(UP.MODELS),
            "name": card.get("name", ""),
        })
        out_4x = out_dir / f"{quality}_1200dpi.png"
        try:
            task = asyncio.create_task(asyncio.to_thread(
                _upscale_native_4x, src_path, out_4x, quality))
            async for item in _run_with_heartbeats_task(task):
                if isinstance(item, tuple) and item and item[0] == "__result__":
                    pass
                else:
                    yield item
        except UP.UpscalerNotAvailable as e:
            results.append({
                "quality": quality,
                "model": model_spec.ncnn_name,
                "description": model_spec.description,
                "unavailable": str(e),
                "url_1200": None, "url_600": None,
                "dpi_1200": None, "dpi_600": None,
            })
            continue
        except Exception as e:
            log.exception("upscale-test: %s failed", quality)
            results.append({
                "quality": quality,
                "model": model_spec.ncnn_name,
                "description": model_spec.description,
                "error": str(e),
                "url_1200": None, "url_600": None,
                "dpi_1200": None, "dpi_600": None,
            })
            continue

        yield _sse("progress", {
            "phase": "downsample",
            "quality": quality,
            "index": idx, "total": len(UP.MODELS),
            "name": card.get("name", ""),
        })
        out_600 = out_dir / f"{quality}_600dpi.png"
        try:
            await asyncio.to_thread(
                _downsample_to_2x, out_4x, out_600, src_path)
        except Exception as e:
            log.exception("upscale-test: downsample %s failed", quality)
            results.append({
                "quality": quality,
                "model": model_spec.ncnn_name,
                "description": model_spec.description,
                "error": f"downsample failed: {e}",
                "url_1200": f"/output/upscale-tests/{scryfall_id}/{out_4x.name}",
                "url_600": None,
                "dpi_1200": _effective_dpi(out_4x),
                "dpi_600": None,
            })
            continue

        results.append({
            "quality": quality,
            "model": model_spec.ncnn_name,
            "description": model_spec.description,
            "url_1200": f"/output/upscale-tests/{scryfall_id}/{out_4x.name}",
            "url_600": f"/output/upscale-tests/{scryfall_id}/{out_600.name}",
            "dpi_1200": _effective_dpi(out_4x),
            "dpi_600": _effective_dpi(out_600),
        })

    yield _sse("done", {
        "scryfall_id": scryfall_id,
        "name": card.get("name", ""),
        "results": results,
        "source_url": f"/output/upscale-tests/{scryfall_id}/../../..",  # unused, informational
    })


def _effective_dpi(image_path: Path) -> dict[str, float]:
    try:
        dpi_x, dpi_y = UP.effective_dpi_of(image_path)
    except Exception:
        return {"x": 0.0, "y": 0.0}
    return {"x": round(dpi_x, 1), "y": round(dpi_y, 1)}


async def _export_stream(state: AppState, *, project_name: str,
                          spec: PageSpec,
                          cut_color: tuple[float, float, float],
                          upscale_on: bool,
                          quality: str = "quality",
                          backs_mode: str = "none",
                          flip_edge: str = "long",
                          back_offset: tuple[float, float] = (0.0, 0.0),
                          output_format: str = "pdf",
                          png_dpi: int = 300,
                          dpi_target: int = 600,
                          ) -> AsyncIterator[bytes]:
    def _sse(event: str, data: dict[str, Any]) -> bytes:
        return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()

    # 600 DPI is scale=2 (native 4× downsampled); 1200 DPI keeps the raw
    # 4× output. Cache keys already include scale, so the two targets don't
    # collide.
    upscale_scale = 4 if dpi_target >= 1200 else 2
    # Below this per-axis DPI, an image would look under-rendered relative to
    # the requested target and should be upscaled. ~90 % of target keeps a
    # small headroom for images that are just under nominal size.
    upscale_min_dpi = dpi_target * 0.9

    upscaler: UP.UpscaleBackend | None = None
    if upscale_on:
        try:
            upscaler = UP.select_upscaler("auto", quality=quality)
        except UP.UpscalerNotAvailable as e:
            yield _sse("error", {"message":
                f"Upscaling requested but no backend is installed: {e}. "
                "Run `python cli.py setup-upscaler`, or export with upscale=false."})
            return

    # If the user explicitly opted out of upscaling, don't hard-fail their
    # export on the DPI gate — warn only. They know they're going lo-fi.
    dpi_fail = 290.0 if upscale_on else 0.0

    try:
        project = Project.load(project_name, state.projects_dir)
    except FileNotFoundError:
        yield _sse("error", {"message": f"Project {project_name!r} not found"})
        return

    # Declared outside the try so the finally can always safely reference
    # it, even if the export bails before any prefetch has been scheduled.
    pending: dict[int, "asyncio.Task"] = {}

    try:
        total = len(project.entries)
        yield _sse("start", {"total": total, "project": project.name,
                             "upscale": upscale_on, "quality": quality,
                             "dpi_target": dpi_target})

        # --- Download pipeline setup -----------------------------------------
        # Prefetching the next card's download while we upscale the current
        # one gives us a free win on cold-cache exports — the Scryfall image
        # for card N+1 arrives before we're ready to upscale it.
        def _download_for(entry):
            card = state.client.resolve_named(
                entry.name, entry.selected_print.set,
                entry.selected_print.collector_number,
            )
            front, _back = state.client.face_images_for(card)
            return front, state.client.download_image(front)

        def _prefetch_from(start_idx: int) -> None:
            """Kick off the download for the next non-custom entry at or
            after `start_idx`, unless one is already pending."""
            for i in range(start_idx, total):
                if project.entries[i].custom_image_path:
                    continue
                if i in pending:
                    return
                pending[i] = asyncio.create_task(
                    asyncio.to_thread(_download_for, project.entries[i]))
                return

        _prefetch_from(0)

        render_cards: list[RenderCard] = []
        for idx, entry in enumerate(project.entries):
            # Custom uploaded art — same DPI target as Scryfall, but we skip
            # the download step and only upscale when the source is below
            # the ~600 DPI threshold (no point re-processing an already-4K
            # image the user brought themselves).
            if entry.custom_image_path:
                yield _sse("progress", {"index": idx, "total": total,
                                        "name": entry.name, "phase": "custom"})
                custom_path = Path(entry.custom_image_path)
                if not custom_path.exists():
                    yield _sse("error", {"index": idx, "name": entry.name,
                                          "message": f"missing file {custom_path}"})
                    return

                image_path = custom_path
                if upscaler is not None and UP.needs_upscale(
                    custom_path, min_dpi=upscale_min_dpi,
                ):
                    yield _sse("progress", {"index": idx, "total": total,
                                            "name": entry.name, "phase": "upscale"})
                    cache_key = f"custom-{project_name}-{custom_path.stem}"
                    try:
                        async for item in _run_with_heartbeats(
                            _upscale_kw, custom_path, cache_key, 0, quality,
                            upscale_scale,
                        ):
                            if isinstance(item, tuple) and item and item[0] == "__result__":
                                image_path = item[1]
                            else:
                                yield item
                    except Exception as e:
                        yield _sse("error", {"index": idx, "name": entry.name,
                                              "message": f"upscale failed: {e}"})
                        return

                try:
                    await asyncio.to_thread(UP.check_dpi_gate, image_path,
                                            fail=dpi_fail, label=entry.name)
                except ValueError as e:
                    yield _sse("error", {"index": idx, "name": entry.name,
                                          "message": str(e)})
                    return

                # Custom entries still need a back if the user asked for
                # duplex or separate — without this the render step barfs
                # with "these cards have no back image resolved".
                custom_back = None
                if backs_mode != "none":
                    try:
                        custom_back = await asyncio.to_thread(
                            _resolve_back, entry, state.client, upscaler,
                            project.default_back_filename, upscale_scale)
                    except BK.BackResolutionError as e:
                        yield _sse("error", {"index": idx, "name": entry.name,
                                              "message": str(e)})
                        return

                render_cards.append(RenderCard(image_path=image_path,
                                                name=entry.name,
                                                quantity=entry.quantity,
                                                back_image_path=custom_back))
                continue

            yield _sse("progress", {"index": idx, "total": total,
                                    "name": entry.name, "phase": "download"})

            # Await the download that was kicked off earlier (either at loop
            # start or by the previous iteration's prefetch).
            task = pending.pop(idx, None)
            if task is None:
                # Custom-only earlier entries meant we hadn't started this one;
                # do it now with a heartbeat wrapper.
                task = asyncio.create_task(
                    asyncio.to_thread(_download_for, entry))
            try:
                dl_result = None
                async for item in _run_with_heartbeats_task(task):
                    if isinstance(item, tuple) and item and item[0] == "__result__":
                        dl_result = item[1]
                    else:
                        yield item
                front, src_path = dl_result
            except SF.ScryfallError as e:
                yield _sse("error", {"index": idx, "name": entry.name, "message": str(e)})
                return

            # Kick off the NEXT non-custom download in parallel so it lands
            # during our upscale step.
            _prefetch_from(idx + 1)

            image_path = src_path
            if upscaler is not None:
                yield _sse("progress", {"index": idx, "total": total,
                                        "name": entry.name, "phase": "upscale"})
                try:
                    async for item in _run_with_heartbeats(
                        _upscale_kw, src_path, front.scryfall_id,
                        front.face_index, quality, upscale_scale,
                    ):
                        if isinstance(item, tuple) and item and item[0] == "__result__":
                            image_path = item[1]
                        else:
                            yield item
                except Exception as e:
                    yield _sse("error", {"index": idx, "name": entry.name,
                                          "message": f"upscale failed: {e}"})
                    return

            try:
                async for item in _run_with_heartbeats(
                    _dpi_gate_kw, image_path, entry.name,
                ):
                    if isinstance(item, tuple) and item and item[0] == "__result__":
                        pass
                    else:
                        yield item
            except ValueError as e:
                yield _sse("error", {"index": idx, "name": entry.name,
                                      "message": str(e)})
                return

            back_path = None
            if backs_mode != "none":
                yield _sse("progress", {"index": idx, "total": total,
                                        "name": entry.name, "phase": "back"})
                try:
                    async for item in _run_with_heartbeats(
                        _resolve_back, entry, state.client, upscaler,
                        project.default_back_filename, upscale_scale,
                    ):
                        if isinstance(item, tuple) and item and item[0] == "__result__":
                            back_path = item[1]
                        else:
                            yield item
                except BK.BackResolutionError as e:
                    yield _sse("error", {"index": idx, "name": entry.name,
                                          "message": str(e)})
                    return

            render_cards.append(RenderCard(image_path=image_path,
                                            name=entry.name,
                                            quantity=entry.quantity,
                                            back_image_path=back_path))

        yield _sse("progress", {"index": total, "total": total,
                                "name": "", "phase": "render"})

        # Timestamped filenames so each export keeps a full history — the
        # download button in the UI references whatever the render step
        # returns, so old files remain accessible via `output/`.
        out_path = default_output_path(project.name, backs_mode)  # type: ignore[arg-type]

        def _render() -> tuple[Path, Path | None]:
            return render_pdf(
                render_cards, out_path,
                project_name=project.name,
                spec=spec,
                backs_mode=backs_mode,  # type: ignore[arg-type]
                flip_edge=flip_edge,     # type: ignore[arg-type]
                back_offset_x_mm=back_offset[0],
                back_offset_y_mm=back_offset[1],
                cut_color=cut_color,
                # We already ran the DPI gate per-image above; keep the
                # render's own gate loose so it doesn't second-guess us.
                dpi_warn=290.0, dpi_fail=0.0,
            )

        fronts_path, backs_path = None, None
        async for item in _run_with_heartbeats(_render):
            if isinstance(item, tuple) and item and item[0] == "__result__":
                fronts_path, backs_path = item[1]
            else:
                yield item

        if output_format == "png":
            yield _sse("progress", {"index": total, "total": total,
                                    "name": "", "phase": "rasterise"})

            # Route PNGs into `output/{project_slug}/` so a deck's pages
            # land grouped together on disk — much friendlier than 6-10
            # loose files at the top of output/.
            png_dir = _project_output_dir(project.name)

            def _rasterise() -> tuple[list[Path], list[Path]]:
                fronts_pngs = rasterise_pdf_to_pngs(
                    fronts_path, dpi=png_dpi, out_dir=png_dir)  # type: ignore[arg-type]
                backs_pngs = (
                    rasterise_pdf_to_pngs(
                        backs_path, dpi=png_dpi, out_dir=png_dir)  # type: ignore[arg-type]
                    if backs_path else []
                )
                return fronts_pngs, backs_pngs

            fronts_pngs, backs_pngs = [], []
            async for item in _run_with_heartbeats(_rasterise):
                if isinstance(item, tuple) and item and item[0] == "__result__":
                    fronts_pngs, backs_pngs = item[1]
                else:
                    yield item
            yield _sse("done", {
                "format": "png",
                "png_paths": [str(p) for p in fronts_pngs],
                "backs_png_paths": [str(p) for p in backs_pngs],
                # Absolute + display-friendly folder path so the UI can
                # tell the user where to look on disk.
                "output_dir": str(png_dir),
                "output_dir_abs": str(png_dir.resolve()),
                # Handy for users who want the source too.
                "path": str(fronts_path),
                "backs_path": str(backs_path) if backs_path else None,
                "page_count": len(fronts_pngs),
                "backs_page_count": len(backs_pngs),
            })
        else:
            page_count = pdf_page_count(fronts_path) if fronts_path else 0
            backs_page_count = (pdf_page_count(backs_path)
                                if backs_path else 0)
            yield _sse("done", {"format": "pdf",
                                 "path": str(fronts_path),
                                 "backs_path": str(backs_path) if backs_path else None,
                                 "page_count": page_count,
                                 "backs_page_count": backs_page_count})

    except Exception as e:
        log.exception("Export failed")
        yield _sse("error", {"message": str(e)})
    finally:
        # Whether the stream finished cleanly, errored, or the client
        # disconnected mid-flight, any prefetched download tasks that never
        # got awaited are still holding onto the thread pool + a socket
        # connection to Scryfall. Cancel them so they don't leak.
        for task in list(pending.values()):
            if not task.done():
                task.cancel()
