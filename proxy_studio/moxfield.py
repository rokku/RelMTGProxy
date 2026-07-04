"""Moxfield deck import.

Accepts a full Moxfield URL (moxfield.com/decks/{id}) or a bare deck ID and
returns `DeckEntry` records that plug into the existing project-creation
flow.

Moxfield's public deck API is unauthenticated but expects a normal browser
User-Agent. It has migrated across two shapes over time — this module tries
the newer v3 endpoint first and falls back to v2 automatically.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import requests

from .decklist import DeckEntry

log = logging.getLogger(__name__)

# Accepts:
#   https://www.moxfield.com/decks/ABC123-xyz
#   http://moxfield.com/decks/ABC123
#   moxfield.com/decks/ABC123/anything
_URL_RE = re.compile(
    r"^(?:https?://)?(?:www\.)?moxfield\.com/decks/([A-Za-z0-9_-]+)/?",
    re.IGNORECASE,
)

# Bare deck IDs are 10+ chars of URL-safe alphabet — Moxfield uses NanoID.
# Requiring ≥10 chars keeps card names like "Duress" from being mistaken
# for deck IDs.
_BARE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{10,}$")

API_V3 = "https://api2.moxfield.com/v3/decks/all/{id}"
API_V2 = "https://api.moxfield.com/v2/decks/all/{id}"

# Moxfield's edge blocks obvious library clients — send full browser-like
# headers. `Origin`/`Referer` in particular seem to be checked.
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


def _browser_headers(deck_id: str) -> dict[str, str]:
    return {
        "User-Agent": _UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": "https://www.moxfield.com",
        "Referer": f"https://www.moxfield.com/decks/{deck_id}",
        "sec-ch-ua": '"Chromium";v="126", "Google Chrome";v="126", "Not-A.Brand";v="99"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"macOS"',
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-site",
    }


class MoxfieldError(RuntimeError):
    pass


def looks_like_moxfield(text: str) -> bool:
    """True if `text` is a Moxfield URL or bare deck ID (best-effort)."""
    s = (text or "").strip()
    if not s:
        return False
    if _URL_RE.match(s):
        return True
    # Bare ID: single-line, no whitespace, matches NanoID shape.
    if "\n" in s or " " in s:
        return False
    return bool(_BARE_ID_RE.match(s))


def extract_deck_id(text: str) -> str:
    s = (text or "").strip()
    m = _URL_RE.match(s)
    if m:
        return m.group(1)
    if _BARE_ID_RE.match(s):
        return s
    raise MoxfieldError(f"Not a Moxfield URL or deck ID: {s!r}")


def fetch_deck(url_or_id: str, *,
                session: requests.Session | None = None,
                ) -> tuple[str, list[DeckEntry]]:
    """Fetch and parse a public Moxfield deck.

    Returns `(deck_name, entries)`. Private decks or bad IDs raise
    MoxfieldError with a message suitable for showing to the user.
    """
    deck_id = extract_deck_id(url_or_id)
    session = session or requests.Session()
    headers = _browser_headers(deck_id)

    last_err: str | None = None
    for tmpl in (API_V3, API_V2):
        url = tmpl.format(id=deck_id)
        log.info("Fetching Moxfield deck: %s", url)
        try:
            resp = session.get(url, headers=headers, timeout=30)
        except requests.RequestException as e:
            last_err = f"network error: {e}"
            continue
        if resp.status_code == 200:
            try:
                return _parse_response(resp.json())
            except ValueError as e:
                # Fell through to next endpoint if parse fails.
                last_err = f"could not parse response: {e}"
                continue
        if resp.status_code == 404:
            last_err = "deck not found — is it public?"
            continue
        last_err = f"{resp.status_code} from Moxfield"
    raise MoxfieldError(f"Could not fetch deck {deck_id!r}: {last_err}")


# --- Response parsing ------------------------------------------------------

def _parse_response(data: dict[str, Any]) -> tuple[str, list[DeckEntry]]:
    name = data.get("name") or "Moxfield deck"
    entries: list[DeckEntry] = []

    # v3 shape: {"boards": {"mainboard": {"cards": {key: {quantity, card: {...}}}}}}
    if "boards" in data and isinstance(data["boards"], dict):
        boards = data["boards"]
        for board_name in ("commanders", "mainboard", "companions"):
            b = boards.get(board_name) or {}
            entries.extend(_extract_v3(b.get("cards") or {}))
    else:
        # v2 shape: top-level "mainboard", "commanders", etc.
        for board_name in ("commanders", "mainboard", "companions"):
            b = data.get(board_name) or {}
            entries.extend(_extract_v2(b))

    if not entries:
        raise MoxfieldError(f"Deck {name!r} contained no cards")
    return name, entries


def _extract_v3(cards: dict[str, Any]) -> list[DeckEntry]:
    out: list[DeckEntry] = []
    for _key, item in cards.items():
        card = item.get("card") or {}
        name = card.get("name")
        if not name:
            continue
        out.append(DeckEntry(
            quantity=int(item.get("quantity", 1)),
            name=name,
            set_code=(card.get("set") or "").lower() or None,
            collector_number=card.get("cn") or None,
        ))
    return out


def _extract_v2(board: dict[str, Any]) -> list[DeckEntry]:
    # v2 keys the board by card name; each value has quantity + card.
    out: list[DeckEntry] = []
    for _key, item in board.items():
        card = item.get("card") or {}
        name = card.get("name") or _key
        if not name:
            continue
        out.append(DeckEntry(
            quantity=int(item.get("quantity", 1)),
            name=name,
            set_code=(card.get("set") or "").lower() or None,
            collector_number=card.get("cn") or None,
        ))
    return out


# --- Utilities -------------------------------------------------------------

def sanitize_project_name(raw: str, fallback: str = "moxfield-deck") -> str:
    """Turn a Moxfield deck name into something the server will accept.

    The server allows a permissive character set (commander names like
    "Cass, Hand of Vengeance" or "K'rrik, Son of Yawgmoth" are fine).
    Only filesystem-hostile characters and control chars need scrubbing.
    """
    if not raw:
        return fallback
    # Strip only filesystem-hostile chars (matches server's blocklist).
    hostile = set('/\\<>:"|?*')
    cleaned = "".join(" " if (c in hostile or ord(c) < 0x20) else c for c in raw)
    # Collapse runs of whitespace.
    cleaned = " ".join(cleaned.split())
    # Trim leading punctuation so the first char is alphanumeric.
    while cleaned and not cleaned[0].isalnum():
        cleaned = cleaned[1:].lstrip()
    # Trim trailing whitespace only (commas etc. at the end are fine).
    cleaned = cleaned.rstrip()
    if not cleaned:
        return fallback
    return cleaned[:64]
