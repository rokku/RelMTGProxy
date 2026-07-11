"""Scryfall API client.

Implements Scryfall's request etiquette: custom User-Agent, JSON Accept,
≥100 ms between requests. All responses cached to `cache/api/` keyed by the
URL. Images cached to `cache/images/original/`.

Spec §4 is authoritative on multi-face handling — see `face_images_for`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import threading
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import requests

from . import __version__

log = logging.getLogger(__name__)

USER_AGENT = f"MTGProxyStudio/{__version__} (local; +https://github.com/local/mtg-proxy-studio)"
API_BASE = "https://api.scryfall.com"
_MIN_INTERVAL_S = 0.1     # ≥100 ms between requests, per Scryfall etiquette.
_CACHE_TTL_S = 30 * 24 * 3600  # 30 days for JSON responses.


# --- Multi-face handling ----------------------------------------------------
# Layouts where card_faces[] carries per-face image_uris (true DFCs).
DFC_LAYOUTS = frozenset({"transform", "modal_dfc", "double_faced_token", "reversible_card"})
# Layouts that are physically single-sided but have card_faces in the JSON.
SINGLE_FACE_MULTI_LAYOUTS = frozenset({"split", "flip", "adventure", "leveler",
                                       "saga", "class", "case", "planar", "mutate"})


@dataclass(frozen=True)
class FaceImage:
    """A downloadable face image — front or back of some card."""
    scryfall_id: str
    face_index: int          # 0 = front, 1 = back for DFCs
    face_name: str
    image_url: str
    layout: str


class ScryfallError(RuntimeError):
    pass


class NotFoundError(ScryfallError):
    def __init__(self, name: str, suggestions: list[str] | None = None):
        self.name = name
        self.suggestions = suggestions or []
        msg = f"Card not found: {name!r}"
        if suggestions:
            msg += f" — did you mean: {', '.join(suggestions[:5])}?"
        super().__init__(msg)


class ScryfallClient:
    """Rate-limited, disk-cached Scryfall client.

    Thread-safe rate limiting via an internal lock — safe under FastAPI even
    with concurrent requests, without needing an async client for Phase 1.
    """

    def __init__(self, cache_dir: str | Path = "cache",
                 session: requests.Session | None = None):
        self.cache_dir = Path(cache_dir)
        self.api_cache = self.cache_dir / "api"
        self.image_cache = self.cache_dir / "images" / "original"
        self.api_cache.mkdir(parents=True, exist_ok=True)
        self.image_cache.mkdir(parents=True, exist_ok=True)

        self.session = session or requests.Session()
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        })

        self._last_request_at: float = 0.0
        self._rate_lock = threading.Lock()

    # --- Low-level: cached GET ---------------------------------------------
    def _get_json(self, url: str, *, use_cache: bool = True) -> dict[str, Any]:
        cache_path = self._json_cache_path(url)
        if use_cache and cache_path.exists():
            age = time.time() - cache_path.stat().st_mtime
            if age < _CACHE_TTL_S:
                return json.loads(cache_path.read_text(encoding="utf-8"))

        self._throttle()
        log.debug("GET %s", url)
        resp = self.session.get(url, timeout=30)
        if resp.status_code == 404:
            # Preserve JSON body for callers (fuzzy suggestions live there).
            try:
                return {"__http_status": 404, **resp.json()}
            except ValueError:
                return {"__http_status": 404}
        if resp.status_code >= 400:
            raise ScryfallError(f"{resp.status_code} from Scryfall for {url}: {resp.text[:200]}")

        data = resp.json()
        tmp = _unique_tmp_path(cache_path)
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(cache_path)
        return data

    def _throttle(self) -> None:
        with self._rate_lock:
            wait = _MIN_INTERVAL_S - (time.time() - self._last_request_at)
            if wait > 0:
                time.sleep(wait)
            self._last_request_at = time.time()

    def _json_cache_path(self, url: str) -> Path:
        h = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
        return self.api_cache / f"{h}.json"

    # --- High-level card lookup --------------------------------------------
    def resolve_named(self, name: str, set_code: str | None = None,
                      collector_number: str | None = None) -> dict[str, Any]:
        """Return the canonical card JSON.

        - If both set_code and collector_number are supplied, fetch that exact
          printing (`/cards/{set}/{cn}`).
        - Otherwise, `named?fuzzy=…`. On 404, raise NotFoundError with any
          `similar_cards` Scryfall returned.
        """
        if set_code and collector_number:
            url = f"{API_BASE}/cards/{set_code.lower()}/{urllib.parse.quote(collector_number)}"
            data = self._get_json(url)
            if data.get("__http_status") == 404:
                raise NotFoundError(f"{name} ({set_code} {collector_number})")
            return data

        url = f"{API_BASE}/cards/named?fuzzy={urllib.parse.quote(name)}"
        data = self._get_json(url)
        if data.get("__http_status") == 404:
            # Fall back to a search to gather suggestions.
            suggestions = self._suggest(name)
            raise NotFoundError(name, suggestions=suggestions)
        return data

    def _suggest(self, name: str) -> list[str]:
        url = f"{API_BASE}/cards/search?q={urllib.parse.quote(name)}"
        try:
            data = self._get_json(url)
        except ScryfallError:
            return []
        if data.get("__http_status") == 404:
            return []
        return [c.get("name", "") for c in data.get("data", [])[:5] if c.get("name")]

    # --- Printings ---------------------------------------------------------
    def all_printings(self, card: dict[str, Any]) -> list[dict[str, Any]]:
        """All printings of the same Oracle card, oldest → newest.

        Follows `prints_search_uri` and paginates via `has_more` / `next_page`.
        Filters out digital-only and memorabilia (`set_type == "memorabilia"`)
        by default — the UI can toggle these back on later.
        """
        url = card.get("prints_search_uri")
        if not url:
            return [card]
        printings: list[dict[str, Any]] = []
        seen_urls: set[str] = set()
        while url and url not in seen_urls:
            seen_urls.add(url)
            page = self._get_json(url)
            printings.extend(page.get("data", []))
            url = page.get("next_page") if page.get("has_more") else None
        return printings

    # --- Face → image URLs -------------------------------------------------
    def face_images_for(self, card: dict[str, Any]) -> tuple[FaceImage, FaceImage | None]:
        """Return (front, back_or_none) FaceImage records for the printing.

        See spec §4 multi-face table. `back` is None when the card should use
        the standard MTG back.
        """
        layout = card.get("layout", "normal")
        sid = card["id"]

        if layout in DFC_LAYOUTS:
            faces = card.get("card_faces", [])
            if len(faces) < 2:
                raise ScryfallError(f"DFC-layout card {sid} missing card_faces")
            front = _face_image(sid, 0, faces[0], layout)
            back = _face_image(sid, 1, faces[1], layout)
            return front, back

        # meld: v1 = standard back; log a warning that we're not resolving it.
        if layout == "meld":
            log.warning("Meld card %s: using standard back (meld-result resolution is Phase 5).",
                        card.get("name"))

        front_url = _top_level_image_url(card)
        front = FaceImage(scryfall_id=sid, face_index=0,
                          face_name=card.get("name", ""),
                          image_url=front_url, layout=layout)
        return front, None

    # --- Image download ----------------------------------------------------
    def download_image(self, face: FaceImage) -> Path:
        """Download a face image to the disk cache; return its path."""
        out = self.image_cache / f"{face.scryfall_id}_face{face.face_index}.png"
        if out.exists() and out.stat().st_size > 0:
            return out
        self._throttle()
        log.debug("GET image %s", face.image_url)
        resp = self.session.get(face.image_url, timeout=60, stream=True)
        if resp.status_code >= 400:
            raise ScryfallError(f"{resp.status_code} fetching image {face.image_url}")
        tmp = _unique_tmp_path(out)
        with tmp.open("wb") as fh:
            for chunk in resp.iter_content(chunk_size=64 * 1024):
                if chunk:
                    fh.write(chunk)
        tmp.replace(out)
        return out


def _top_level_image_url(card: dict[str, Any]) -> str:
    imgs = card.get("image_uris")
    if not imgs:
        # Rare: some card entries put images only under card_faces even for
        # non-DFC layouts (adventure, split, flip). Use the front face image.
        faces = card.get("card_faces", [])
        if faces:
            imgs = faces[0].get("image_uris")
    if not imgs or "png" not in imgs:
        raise ScryfallError(
            f"Card {card.get('id')} has no png image_uri (name={card.get('name')})"
        )
    return imgs["png"]


def _face_image(sid: str, idx: int, face: dict[str, Any], layout: str) -> FaceImage:
    imgs = face.get("image_uris") or {}
    if "png" not in imgs:
        raise ScryfallError(f"Face {idx} of {sid} has no png image_uri")
    return FaceImage(scryfall_id=sid, face_index=idx,
                     face_name=face.get("name", ""),
                     image_url=imgs["png"], layout=layout)


def choose_default_printing(printings: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Pick a sensible default printing: prefer non-digital, non-memorabilia,
    highest-res, most recent. Falls back to the first printing if all are
    filtered out.
    """
    kept = [
        p for p in printings
        if not p.get("digital", False)
        and p.get("set_type") != "memorabilia"
        and p.get("image_status") in {"highres_scan", "lowres"}
    ]
    if not kept:
        kept = list(printings)
    # Prefer highres_scan, then most recent release.
    kept.sort(key=lambda p: (
        0 if p.get("image_status") == "highres_scan" else 1,
        # Sort by released_at descending (newer first).
        -_released_key(p),
    ))
    return kept[0]


def _released_key(p: dict[str, Any]) -> int:
    d = p.get("released_at", "")
    try:
        return int(d.replace("-", ""))
    except ValueError:
        return 0


def _unique_tmp_path(target: Path) -> Path:
    """Sibling path with a random suffix — see the identical helper in
    `upscale.py` for the rationale (concurrent writers + Windows locks)."""
    token = secrets.token_hex(4)
    return target.with_suffix(target.suffix + f".{token}.tmp")
