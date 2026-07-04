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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import backs as BK
from . import decklist as DL
from . import moxfield as MX
from . import scryfall as SF
from . import upscale as UP
from .layout import PageSpec
from .pdf_export import (
    DEFAULT_CUT_COLOR, RenderCard, default_output_path, parse_hex_color,
    render_pdf,
)
from .project import Entry, Project, SelectedPrint

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent.parent / "static"
ASSETS_DIR = Path(__file__).parent.parent / "assets"
OUTPUT_DIR = Path(__file__).parent.parent / "output"
UPLOADS_ROOT = Path(__file__).parent.parent / "cache" / "images" / "custom"

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


@dataclass
class AppState:
    projects_dir: Path
    client: SF.ScryfallClient
    # Oracle-level printings cache — safe to share across projects because
    # printings only depend on the oracle_id.
    printings_cache: dict[str, list[dict[str, Any]]] = field(default_factory=dict)


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
        state: AppState = app.state.picker

        # Three ways to seed a project:
        #   1. Moxfield URL / bare deck ID
        #   2. Plain-text decklist
        #   3. Neither — start empty, add cards via uploads or `add`
        decklist_stripped = (req.decklist or "").strip()
        entries: list[DL.DeckEntry] = []
        if MX.looks_like_moxfield(decklist_stripped):
            try:
                mox_name, entries = MX.fetch_deck(decklist_stripped)
            except MX.MoxfieldError as e:
                raise HTTPException(400, f"Moxfield import failed: {e}")
            raw_name = (req.name or "").strip() or MX.sanitize_project_name(mox_name)
        elif decklist_stripped:
            try:
                entries = DL.parse_text(decklist_stripped)
            except DL.DecklistError as e:
                raise HTTPException(400, {"error": "decklist parse failed",
                                           "failures": e.failures})
            raw_name = req.name
        else:
            raw_name = req.name

        name = _validate_name(raw_name)
        target = state.projects_dir / f"{name}.json"
        if target.exists():
            raise HTTPException(409, f"Project {name!r} already exists")

        project = Project(name=name)
        failures: list[dict[str, str]] = []
        for de in entries:
            try:
                card = state.client.resolve_named(
                    de.name, de.set_code, de.collector_number)
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
            except SF.NotFoundError as e:
                failures.append({"name": de.name, "message": str(e)})
            except SF.ScryfallError as e:
                failures.append({"name": de.name, "message": str(e)})

        project.save(state.projects_dir)
        return {
            "name": name,
            "entries": len(project.entries),
            "failures": failures,
        }

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
        if "/" in safe or "\\" in safe or safe.startswith("."):
            raise HTTPException(400, "invalid filename")
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
        if "/" in safe or "\\" in safe or safe.startswith("."):
            raise HTTPException(400, "invalid filename")
        asset = _library_dir() / safe
        if not asset.exists():
            raise HTTPException(404, f"library asset {req.filename!r} not found")
        entry.custom_image_path = f"cache/images/custom/{LIBRARY_DIRNAME}/{asset.name}"
        project.save(state.projects_dir)
        thumb_url = f"/uploads/{LIBRARY_DIRNAME}/{asset.name}"
        return {"ok": True, "entry": _entry_view(entry, state, card=None,
                                                   thumb_url_override=thumb_url)}

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
        project.save(state.projects_dir)
        return {"ok": True, "entry": _entry_view(entry, state, card=card)}

    # --- Prints ------------------------------------------------------------
    @app.get("/api/prints/{oracle_id}")
    def get_prints(oracle_id: str) -> dict[str, Any]:
        state: AppState = app.state.picker
        if oracle_id in state.printings_cache:
            printings = state.printings_cache[oracle_id]
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
                    backs: str = "none",
                    flip_edge: str = "long",
                    back_offset_x: float = 0.0,
                    back_offset_y: float = 0.0) -> StreamingResponse:
        if cut_lines not in ("full", "ticks"):
            raise HTTPException(400, "cut_lines must be 'full' or 'ticks'")
        if backs not in ("none", "duplex", "separate"):
            raise HTTPException(400, "backs must be 'none', 'duplex' or 'separate'")
        if flip_edge not in ("long", "short"):
            raise HTTPException(400, "flip_edge must be 'long' or 'short'")
        try:
            color = parse_hex_color(cut_color)
        except ValueError as e:
            raise HTTPException(400, f"cut_color: {e}") from e
        state: AppState = app.state.picker
        project_name = _validate_name(name)
        spec = PageSpec(gutter_mm=gutter, cut_line_mode=cut_lines)  # type: ignore[arg-type]
        want_upscale = UP.any_backend_installed() if upscale is None else upscale
        return StreamingResponse(
            _export_stream(state, project_name=project_name, spec=spec,
                           cut_color=color, upscale_on=want_upscale,
                           backs_mode=backs,
                           flip_edge=flip_edge,  # type: ignore[arg-type]
                           back_offset=(back_offset_x, back_offset_y)),
            media_type="text/event-stream",
        )


# --- Helpers ----------------------------------------------------------------

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
        data = await uf.read()
        if not data:
            continue
        if len(data) > _MAX_UPLOAD_BYTES:
            raise HTTPException(400,
                f"{uf.filename}: file too large "
                f"({len(data) / 1024 / 1024:.1f} MB > "
                f"{_MAX_UPLOAD_BYTES / 1024 / 1024:.0f} MB)")
        safe = _safe_upload_name(uf.filename or f"upload{ext}")
        target = _unique_path(_library_dir() / safe)
        target.write_bytes(data)
        out.append({
            "filename": target.name,
            "path": f"cache/images/custom/{LIBRARY_DIRNAME}/{target.name}",
            "url": f"/uploads/{LIBRARY_DIRNAME}/{target.name}",
            "size": len(data),
        })
    return out


def _safe_upload_name(filename: str) -> str:
    """Sanitise an uploaded file's name to keep it filesystem-safe.

    Replaces path separators + windows-hostile chars with underscores; preserves
    everything else the user typed (they'll see this in the entry list).
    """
    hostile = set('/\\<>:"|?*')
    cleaned = "".join(("_" if (c in hostile or ord(c) < 0x20) else c)
                      for c in filename)
    cleaned = cleaned.strip(" .")
    return cleaned or "upload"


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


def _resolve_back(entry, client, upscaler):
    return BK.resolve_back_image(entry, client=client, upscaler=upscaler)


async def _run_with_heartbeats(fn, *args):
    """Run `fn(*args)` in a thread; yield SSE keep-alive bytes while waiting.

    Returns an async generator: each yielded value is a `bytes` chunk to
    forward to the client, except for the final yielded item which is the
    result wrapped in a one-tuple sentinel `(_Result, value)`.
    Callers consume with `async for item in _run_with_heartbeats(...)`.
    """
    task = asyncio.ensure_future(asyncio.to_thread(fn, *args))
    while not task.done():
        try:
            await asyncio.wait_for(asyncio.shield(task),
                                    timeout=_HEARTBEAT_INTERVAL_S)
            break
        except asyncio.TimeoutError:
            yield b": ping\n\n"
    yield ("__result__", task.result())


async def _export_stream(state: AppState, *, project_name: str,
                          spec: PageSpec,
                          cut_color: tuple[float, float, float],
                          upscale_on: bool,
                          backs_mode: str = "none",
                          flip_edge: str = "long",
                          back_offset: tuple[float, float] = (0.0, 0.0),
                          ) -> AsyncIterator[bytes]:
    def _sse(event: str, data: dict[str, Any]) -> bytes:
        return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()

    upscaler: UP.UpscaleBackend | None = None
    if upscale_on:
        try:
            upscaler = UP.select_upscaler("auto")
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

    try:
        total = len(project.entries)
        yield _sse("start", {"total": total, "project": project.name,
                             "upscale": upscale_on})

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
                if upscaler is not None and UP.needs_upscale(custom_path):
                    yield _sse("progress", {"index": idx, "total": total,
                                            "name": entry.name, "phase": "upscale"})
                    cache_key = f"custom-{project_name}-{custom_path.stem}"
                    try:
                        async for item in _run_with_heartbeats(
                            UP.upscale_image, custom_path, cache_key, 0,
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
                render_cards.append(RenderCard(image_path=image_path,
                                                name=entry.name,
                                                quantity=entry.quantity))
                continue

            yield _sse("progress", {"index": idx, "total": total,
                                    "name": entry.name, "phase": "download"})

            def _download(entry=entry):
                card = state.client.resolve_named(
                    entry.name, entry.selected_print.set,
                    entry.selected_print.collector_number,
                )
                front, _back = state.client.face_images_for(card)
                return front, state.client.download_image(front)

            try:
                dl_result = None
                async for item in _run_with_heartbeats(_download):
                    if isinstance(item, tuple) and item and item[0] == "__result__":
                        dl_result = item[1]
                    else:
                        yield item
                front, src_path = dl_result
            except SF.ScryfallError as e:
                yield _sse("error", {"index": idx, "name": entry.name, "message": str(e)})
                return

            image_path = src_path
            if upscaler is not None:
                yield _sse("progress", {"index": idx, "total": total,
                                        "name": entry.name, "phase": "upscale"})
                try:
                    async for item in _run_with_heartbeats(
                        UP.upscale_image, src_path, front.scryfall_id,
                        front.face_index,
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
        yield _sse("done", {"path": str(fronts_path),
                             "backs_path": str(backs_path) if backs_path else None})

    except Exception as e:
        log.exception("Export failed")
        yield _sse("error", {"message": str(e)})
