"""Archidekt deck import.

Accepts a full Archidekt URL (archidekt.com/decks/{id}[/slug]) or a bare
numeric deck ID and returns `DeckEntry` records that plug into the
existing project-creation flow.

Archidekt's public deck API is unauthenticated. The `/api/decks/{id}/`
endpoint returns full deck contents including cards, categories (which
we use to drop sideboard/maybeboard), and metadata.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import requests

from .decklist import DeckEntry
# Reuse Moxfield's name sanitiser — it's provider-agnostic (only blocks
# filesystem-hostile characters and trims to 64 chars).
from .moxfield import sanitize_project_name  # noqa: F401 (re-exported for symmetry)

log = logging.getLogger(__name__)

# Accepts:
#   https://archidekt.com/decks/15364137
#   https://archidekt.com/decks/15364137/the-elven-kingdom
#   http://www.archidekt.com/decks/15364137/
_URL_RE = re.compile(
    r"^(?:https?://)?(?:www\.)?archidekt\.com/decks/(\d+)(?:/[^\s?#]*)?",
    re.IGNORECASE,
)

# Bare deck IDs are numeric. Requiring ≥6 digits stops small card counts
# (e.g. "12345") from being mistaken for deck IDs while still allowing
# older decks (Archidekt IDs are currently 8 digits).
_BARE_ID_RE = re.compile(r"^\d{6,}$")

API = "https://archidekt.com/api/decks/{id}/"

# Archidekt is less picky than Moxfield about headers but still 403s
# obvious bot UAs, so send a real browser signature.
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# Archidekt category names that mark cards we don't want to print. Case-
# insensitive match; anything else (including custom user categories) is
# treated as mainboard.
_EXCLUDED_CATEGORIES = {
    "sideboard", "maybeboard", "considering", "wishlist", "acquire",
}


def _headers() -> dict[str, str]:
    return {
        "User-Agent": _UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
    }


class ArchidektError(RuntimeError):
    pass


def looks_like_archidekt(text: str) -> bool:
    """True if `text` is an Archidekt URL or bare numeric deck ID."""
    s = (text or "").strip()
    if not s:
        return False
    if _URL_RE.match(s):
        return True
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
    raise ArchidektError(f"Not an Archidekt URL or deck ID: {s!r}")


def fetch_deck(url_or_id: str, *,
                session: requests.Session | None = None,
                ) -> tuple[str, list[DeckEntry]]:
    """Fetch and parse a public Archidekt deck.

    Returns `(deck_name, entries)`. Missing/private decks raise
    `ArchidektError` with a message suitable for the user.
    """
    deck_id = extract_deck_id(url_or_id)
    session = session or requests.Session()
    url = API.format(id=deck_id)
    log.info("Fetching Archidekt deck: %s", url)
    try:
        resp = session.get(url, headers=_headers(), timeout=30)
    except requests.RequestException as e:
        raise ArchidektError(f"network error: {e}") from e

    if resp.status_code == 404:
        raise ArchidektError(
            f"Archidekt deck {deck_id} not found — is it public?")
    if resp.status_code != 200:
        raise ArchidektError(
            f"Archidekt returned HTTP {resp.status_code} for deck {deck_id}")
    try:
        payload = resp.json()
    except ValueError as e:
        raise ArchidektError(f"could not parse Archidekt response: {e}") from e
    # Some errors come back with a 200 status but an "error" field in the
    # body (e.g. staged deletions). Surface those the same way.
    if isinstance(payload, dict) and payload.get("error") and not payload.get("cards"):
        raise ArchidektError(f"Archidekt: {payload['error']}")
    return _parse_response(payload)


# --- Response parsing ------------------------------------------------------

def _parse_response(data: dict[str, Any]) -> tuple[str, list[DeckEntry]]:
    name = data.get("name") or "Archidekt deck"
    raw_cards = data.get("cards") or []
    entries: list[DeckEntry] = []

    for item in raw_cards:
        entry = _card_to_entry(item)
        if entry is not None:
            entries.append(entry)

    if not entries:
        raise ArchidektError(f"Deck {name!r} contained no printable cards")
    return name, entries


def _card_to_entry(item: dict[str, Any]) -> DeckEntry | None:
    """Turn one Archidekt deck-card row into a DeckEntry, or None to skip.

    Skips zero-quantity cards, deleted cards, and cards whose only
    category tags mark them as sideboard/maybeboard-style.
    """
    quantity = int(item.get("quantity", 0) or 0)
    if quantity <= 0:
        return None
    if item.get("deletedAt"):
        return None
    if _is_excluded(item.get("categories")):
        return None

    card = item.get("card") or {}
    oracle = card.get("oracleCard") or {}
    name = oracle.get("name") or card.get("displayName")
    if not name:
        return None
    edition = card.get("edition") or {}
    set_code = (edition.get("editioncode") or "").lower() or None
    collector_number = card.get("collectorNumber") or None
    return DeckEntry(
        quantity=quantity,
        name=name,
        set_code=set_code,
        collector_number=collector_number,
    )


def _is_excluded(categories: Any) -> bool:
    """True if the card's category tags mark it as sideboard-adjacent."""
    if not categories:
        return False
    if isinstance(categories, str):
        categories = [categories]
    if not isinstance(categories, list):
        return False
    lowered = {str(c).strip().lower() for c in categories if c}
    if not lowered:
        return False
    # A card is excluded only if *every* tag it carries is excludable —
    # dual-tagged "Commander, Sideboard" cards are kept as commanders.
    return lowered.issubset(_EXCLUDED_CATEGORIES)
