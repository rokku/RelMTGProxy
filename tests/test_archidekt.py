"""Tests for `proxy_studio.archidekt`.

Real Archidekt API is not called — a fake `requests.Session` is injected
so tests run offline.
"""

from __future__ import annotations

from typing import Any

import pytest

from proxy_studio import archidekt as AK


class FakeResp:
    def __init__(self, status: int, payload: dict[str, Any] | None = None):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.text = "" if payload is not None else "not found"

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses: dict[str, FakeResp]):
        self.responses = responses
        self.calls: list[str] = []
        self.headers: dict[str, str] = {}

    def get(self, url, timeout=None, headers=None, **_kwargs):
        self.calls.append(url)
        for prefix, resp in self.responses.items():
            if url.startswith(prefix):
                return resp
        return FakeResp(404)


def _card(name: str, quantity: int = 1, *,
          set_code: str = "ltc", cn: str = "1",
          categories: list[str] | None = None,
          deleted: bool = False) -> dict[str, Any]:
    return {
        "quantity": quantity,
        "categories": categories or [],
        "deletedAt": "2024-01-01" if deleted else None,
        "card": {
            "collectorNumber": cn,
            "edition": {"editioncode": set_code},
            "oracleCard": {"name": name},
        },
    }


DECK_PAYLOAD = {
    "name": "The Elven Kingdom",
    "cards": [
        _card("Galadriel, Elven-Queen", set_code="LTC", cn="3",
              categories=["Commander"]),
        _card("Llanowar Elves", quantity=4, set_code="m19", cn="314"),
        _card("Sol Ring", quantity=1, set_code="c15"),
        # Should be skipped: sideboard-only.
        _card("Duress", quantity=3, categories=["Sideboard"]),
        # Should be skipped: dual maybeboard/wishlist tags.
        _card("Force of Will", quantity=1, categories=["Maybeboard", "Wishlist"]),
        # Should be skipped: deleted.
        _card("Deleted Card", quantity=1, deleted=True),
        # Should be skipped: zero quantity.
        _card("Nothing", quantity=0),
    ],
}


# --- URL / ID detection ----------------------------------------------------

class TestDetection:
    @pytest.mark.parametrize("raw,expected", [
        ("https://archidekt.com/decks/15364137", "15364137"),
        ("https://archidekt.com/decks/15364137/the-elven-kingdom", "15364137"),
        ("http://www.archidekt.com/decks/15364137/", "15364137"),
        ("ARCHIDEKT.COM/decks/12345678", "12345678"),
        ("15364137", "15364137"),
    ])
    def test_extract(self, raw, expected):
        assert AK.extract_deck_id(raw) == expected

    def test_looks_like_archidekt_positive(self):
        assert AK.looks_like_archidekt(
            "https://archidekt.com/decks/15364137/name")
        assert AK.looks_like_archidekt("15364137")

    @pytest.mark.parametrize("s", [
        "",
        "12345",                  # too short to be an Archidekt ID
        "1 Sol Ring",             # a decklist line
        "1 Sol Ring\n1 Duress",   # multi-line = decklist
        "https://moxfield.com/decks/ABC123def4",  # different provider
        "abc1234567",             # non-digit bare ID (Moxfield-shaped)
    ])
    def test_looks_like_archidekt_negative(self, s):
        assert not AK.looks_like_archidekt(s)


# --- Fetching + parsing ----------------------------------------------------

class TestFetch:
    def test_success(self):
        session = FakeSession({"https://archidekt.com/api/decks/": FakeResp(200, DECK_PAYLOAD)})
        name, entries = AK.fetch_deck("15364137", session=session)
        assert name == "The Elven Kingdom"
        # 3 keepers (Galadriel + Llanowar + Sol Ring); sideboard/maybe/
        # deleted/zero-qty all filtered out.
        assert len(entries) == 3
        by_name = {e.name: e for e in entries}
        assert "Galadriel, Elven-Queen" in by_name
        assert by_name["Galadriel, Elven-Queen"].set_code == "ltc"
        assert by_name["Galadriel, Elven-Queen"].collector_number == "3"
        assert by_name["Llanowar Elves"].quantity == 4

    def test_404_raises_clear_error(self):
        session = FakeSession({})
        with pytest.raises(AK.ArchidektError, match="not found"):
            AK.fetch_deck("99999999", session=session)

    def test_empty_deck_raises(self):
        session = FakeSession({"https://archidekt.com/api/decks/":
                                FakeResp(200, {"name": "empty", "cards": []})})
        with pytest.raises(AK.ArchidektError, match="no printable cards"):
            AK.fetch_deck("15364137", session=session)

    def test_response_uses_display_name_when_oracle_name_missing(self):
        payload = {
            "name": "Fallback name test",
            "cards": [
                {"quantity": 1, "categories": [], "deletedAt": None,
                 "card": {"collectorNumber": "1",
                          "edition": {"editioncode": "abc"},
                          "displayName": "Custom Card",
                          "oracleCard": None}},
            ],
        }
        session = FakeSession({"https://archidekt.com/api/decks/": FakeResp(200, payload)})
        _, entries = AK.fetch_deck("15364137", session=session)
        assert len(entries) == 1
        assert entries[0].name == "Custom Card"

    def test_commander_with_dual_category_is_kept(self):
        """Commanders sometimes carry ['Commander', 'Sideboard'] — those
        should NOT be silently dropped as sideboard cards."""
        payload = {
            "name": "Dual tag test",
            "cards": [
                _card("Krenko, Mob Boss",
                       categories=["Commander", "Sideboard"]),
            ],
        }
        session = FakeSession({"https://archidekt.com/api/decks/": FakeResp(200, payload)})
        _, entries = AK.fetch_deck("15364137", session=session)
        assert [e.name for e in entries] == ["Krenko, Mob Boss"]


# --- Sanitiser -------------------------------------------------------------

class TestSanitizeName:
    def test_reexports_shared_sanitiser(self):
        # Archidekt re-exports Moxfield's sanitiser; verify a couple of
        # representative behaviours so the re-export stays intact.
        assert AK.sanitize_project_name("Cass, Hand of Vengeance") \
            == "Cass, Hand of Vengeance"
        # Filesystem-hostile chars stripped.
        assert AK.sanitize_project_name("A/B\\C") == "A B C"
