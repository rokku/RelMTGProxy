"""Tests for `proxy_studio.moxfield`.

Real Moxfield API is not called — a fake `requests.Session` is injected so
tests run offline.
"""

from __future__ import annotations

from typing import Any

import pytest

from proxy_studio import moxfield as MX


class FakeResp:
    def __init__(self, status: int, payload: dict[str, Any] | None = None):
        self.status_code = status
        self._payload = payload or {}
        self.text = "" if payload else "not found"

    def json(self):
        return self._payload


class FakeSession:
    """Minimal stand-in for `requests.Session`. Supplies canned responses
    keyed by URL prefix."""

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


V3_PAYLOAD = {
    "name": "Test Deck: Aggro/Combo!",
    "boards": {
        "commanders": {
            "cards": {
                "cmd1": {"quantity": 1, "card": {
                    "name": "Mazirek, Kraul Death Priest",
                    "set": "C15", "cn": "48",
                }},
            },
        },
        "mainboard": {
            "cards": {
                "c1": {"quantity": 1, "card": {"name": "Sol Ring", "set": "c15"}},
                "c2": {"quantity": 4, "card": {"name": "Llanowar Elves",
                                                 "set": "m19", "cn": "314"}},
            },
        },
    },
}

V2_PAYLOAD = {
    "name": "Old Format Deck",
    "mainboard": {
        "Sol Ring": {"quantity": 2, "card": {"name": "Sol Ring", "set": "c21"}},
        "Duress":   {"quantity": 3, "card": {"name": "Duress", "set": "m21", "cn": "94"}},
    },
    "commanders": {
        "Mazirek": {"quantity": 1, "card": {"name": "Mazirek, Kraul Death Priest",
                                              "set": "c15", "cn": "48"}},
    },
}


# --- URL / ID detection ----------------------------------------------------

class TestDetection:
    @pytest.mark.parametrize("raw,expected", [
        ("https://www.moxfield.com/decks/AbC-123_xyz", "AbC-123_xyz"),
        ("http://moxfield.com/decks/short10id", "short10id"),
        ("moxfield.com/decks/ABC123def4/", "ABC123def4"),
        ("MOXFIELD.COM/decks/CaseSensitive1", "CaseSensitive1"),
        ("AbC-123_xyz012", "AbC-123_xyz012"),   # bare ID
    ])
    def test_extract(self, raw, expected):
        assert MX.extract_deck_id(raw) == expected

    def test_looks_like_moxfield_positive(self):
        assert MX.looks_like_moxfield("https://moxfield.com/decks/ABCDE12345")
        assert MX.looks_like_moxfield("ABCDE12345XYZ")

    @pytest.mark.parametrize("s", [
        "",
        "1 Sol Ring",           # a decklist line — not a URL / ID
        "Sol Ring",             # card name shorter than min ID length
        "1 Sol Ring\n1 Duress", # multi-line = decklist
        "https://scryfall.com/card/...",  # different domain
    ])
    def test_looks_like_moxfield_negative(self, s):
        assert not MX.looks_like_moxfield(s)


# --- Fetching + parsing ----------------------------------------------------

class TestFetch:
    def test_v3_endpoint_wins_when_available(self):
        session = FakeSession({MX.API_V3.format(id=""): FakeResp(200, V3_PAYLOAD)})
        name, entries = MX.fetch_deck("https://moxfield.com/decks/abcdefghij",
                                       session=session)
        assert name == "Test Deck: Aggro/Combo!"
        # Commander + mainboard, 3 unique cards.
        assert len(entries) == 3
        names = {e.name for e in entries}
        assert "Sol Ring" in names
        assert "Mazirek, Kraul Death Priest" in names
        # v3 set field is uppercase in the payload → normalised to lowercase.
        mazirek = next(e for e in entries if e.name.startswith("Mazirek"))
        assert mazirek.set_code == "c15"
        assert mazirek.collector_number == "48"

    def test_falls_back_to_v2_on_v3_failure(self):
        session = FakeSession({
            MX.API_V3.format(id=""): FakeResp(404),
            MX.API_V2.format(id=""): FakeResp(200, V2_PAYLOAD),
        })
        name, entries = MX.fetch_deck("ABCDEFGHIJ", session=session)
        assert name == "Old Format Deck"
        assert len(entries) == 3
        # Both API endpoints were tried.
        assert any("api2.moxfield.com" in u for u in session.calls)
        assert any("api.moxfield.com/v2" in u for u in session.calls)

    def test_404_on_both_endpoints_raises_clear_error(self):
        session = FakeSession({})  # every URL returns 404 by default
        with pytest.raises(MX.MoxfieldError, match="not found"):
            MX.fetch_deck("ABCDEFGHIJ", session=session)

    def test_empty_deck_raises(self):
        session = FakeSession({MX.API_V3.format(id=""):
                                FakeResp(200, {"name": "empty",
                                                "boards": {}})})
        with pytest.raises(MX.MoxfieldError, match="no cards"):
            MX.fetch_deck("ABCDEFGHIJ", session=session)


# --- Name sanitiser --------------------------------------------------------

class TestSanitizeName:
    @pytest.mark.parametrize("raw,expected", [
        # Filesystem-hostile chars → spaces; whitespace collapsed.
        ("Test Deck: Aggro/Combo!", "Test Deck Aggro Combo!"),
        # Commas, apostrophes, periods are all fine now.
        ("Cass, Hand of Vengeance", "Cass, Hand of Vengeance"),
        ("K'rrik, Son of Yawgmoth", "K'rrik, Son of Yawgmoth"),
        ("Mr. House, President and CEO", "Mr. House, President and CEO"),
        # Leading dashes/dots stripped so the result starts alphanumeric.
        ("---weird---", "weird---"),
        # Empty → fallback.
        ("", "moxfield-deck"),
    ])
    def test_common_cases(self, raw, expected):
        assert MX.sanitize_project_name(raw) == expected

    def test_leading_punct_stripped(self):
        assert MX.sanitize_project_name("_underscore_start_") == "underscore_start_"

    def test_long_names_are_truncated(self):
        raw = "A" * 200
        assert len(MX.sanitize_project_name(raw)) <= 64
