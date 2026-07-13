"""Server smoke tests — cover multi-project CRUD + selection routes.

Scryfall access is stubbed via a fake client so tests run offline.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from proxy_studio.project import Entry, PageSettings, Project, SelectedPrint
from proxy_studio import scryfall as SF
from proxy_studio import server as srv


def _parse_sse_body(body: str) -> list[dict]:
    """Break an SSE response body into `[{event, data}, …]` blocks."""
    out: list[dict] = []
    for block in body.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        event = "message"
        data_lines: list[str] = []
        for line in block.split("\n"):
            if line.startswith(":"):
                continue  # heartbeat
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].strip())
        data = json.loads("".join(data_lines)) if data_lines else {}
        out.append({"event": event, "data": data})
    return out


ORACLE = "oracle-sol-ring"
SR_ID = "print-sol-ring-1"
SR_ID_2 = "print-sol-ring-2"
DURESS_ID = "print-duress"


class FakeClient:
    """Deterministic offline Scryfall stand-in."""

    def __init__(self):
        self.printings = [
            _print(SR_ID, "Sol Ring", ORACLE, "c15", "234", "2015-11-13", "sol-ring-c15"),
            _print(SR_ID_2, "Sol Ring", ORACLE, "c21", "350", "2021-04-23", "sol-ring-c21"),
        ]
        self.duress = _print(DURESS_ID, "Duress", "oracle-duress",
                             "m21", "94", "2020-07-03", "duress")

    # Named-fuzzy + set/CN lookup used by create_project + export.
    def resolve_named(self, name, set_code=None, collector_number=None):
        if set_code and collector_number:
            for p in self.printings + [self.duress]:
                if p["set"] == set_code and p["collector_number"] == collector_number:
                    return p
            raise SF.NotFoundError(name)
        if name.lower() == "sol ring":
            return self.printings[0]
        if name.lower() == "duress":
            return self.duress
        raise SF.NotFoundError(name)

    def _get_json(self, url):
        if "search?q=oracleid" in url:
            if ORACLE in url:
                return {"has_more": False, "data": self.printings}
            return {"has_more": False, "data": [self.duress]}
        # Text search — used by the Scryfall search endpoint.
        if "/cards/search?q=" in url:
            import urllib.parse as _up
            decoded = _up.unquote(url).lower()
            all_cards = self.printings + [self.duress]
            if "t:token" in decoded and "-t:token" not in decoded:
                # Nothing in the fake fixture is a token.
                return {"has_more": False, "data": [], "total_cards": 0}
            hits = [p for p in all_cards if p["name"].lower() in decoded]
            return {"has_more": False, "data": hits, "total_cards": len(hits)}
        for p in self.printings + [self.duress]:
            if f"/cards/{p['id']}" in url:
                return p
        return {"__http_status": 404}


def _print(sid, name, oracle_id, set_code, cn, released, slug):
    return {
        "id": sid, "name": name, "oracle_id": oracle_id,
        "set": set_code, "set_name": f"Set {set_code.upper()}",
        "set_type": "commander",
        "collector_number": cn, "released_at": released,
        "digital": False, "lang": "en",
        "frame": "2015", "frame_effects": [], "border_color": "black",
        "layout": "normal",
        "image_uris": {"normal": f"https://scryfall.example/{slug}.png",
                       "png": f"https://scryfall.example/{slug}.png"},
        "image_status": "highres_scan",
    }


@pytest.fixture
def client(tmp_path):
    app = srv.create_app(projects_dir=tmp_path / "projects",
                         cache_dir=tmp_path / "cache")
    app.state.picker.client = FakeClient()
    return TestClient(app)


@pytest.fixture
def seeded_client(client, tmp_path):
    project = Project(
        name="unit",
        page_settings=PageSettings(),
        entries=[
            Entry(quantity=1, name="Sol Ring", oracle_id=ORACLE,
                  selected_print=SelectedPrint(scryfall_id=SR_ID, set="c15",
                                               collector_number="234"),
                  layout="normal", back="standard"),
            Entry(quantity=2, name="Duress", oracle_id="oracle-duress",
                  selected_print=SelectedPrint(scryfall_id=DURESS_ID, set="m21",
                                               collector_number="94"),
                  layout="normal", back="standard"),
        ],
    )
    project.save(tmp_path / "projects")
    return client


# --- Project collection -----------------------------------------------------

class TestProjectList:
    def test_empty(self, client):
        r = client.get("/api/projects")
        assert r.status_code == 200 and r.json() == []

    def test_lists_saved_projects(self, seeded_client):
        r = seeded_client.get("/api/projects")
        assert r.status_code == 200
        body = r.json()
        assert len(body) == 1
        assert body[0]["name"] == "unit"
        assert body[0]["entries"] == 2


class TestProjectCreate:
    def test_creates_from_decklist(self, client):
        r = client.post("/api/projects", json={
            "name": "fresh",
            "decklist": "1 Sol Ring\n2 Duress\n",
        })
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["name"] == "fresh"
        assert body["entries"] == 2
        assert body["failures"] == []

        # Now readable via GET.
        r2 = client.get("/api/projects/fresh")
        assert r2.status_code == 200

    def test_rejects_duplicate(self, seeded_client):
        r = seeded_client.post("/api/projects", json={
            "name": "unit", "decklist": "1 Sol Ring\n",
        })
        assert r.status_code == 409

    def test_rejects_bad_name(self, client):
        r = client.post("/api/projects", json={
            "name": "../evil", "decklist": "1 Sol Ring\n",
        })
        assert r.status_code == 400

    def test_empty_decklist_creates_empty_project(self, client):
        # A blank decklist is legal now — the user may be starting a
        # bring-your-own-art project and will upload cards next.
        r = client.post("/api/projects", json={
            "name": "empty-shell", "decklist": "  \n// only comments\n",
        })
        assert r.status_code == 201
        assert r.json()["entries"] == 0

    def test_reports_decklist_parse_failures(self, client):
        r = client.post("/api/projects", json={
            "name": "broken", "decklist": "1 Sol Ring\n?????\n",
        })
        assert r.status_code == 400
        body = r.json()
        # FastAPI wraps HTTPException detail in {"detail": ...}.
        assert "failures" in body["detail"]

    def test_reports_card_resolution_failures(self, client):
        r = client.post("/api/projects", json={
            "name": "partial", "decklist": "1 Sol Ring\n1 Unknown Card\n",
        })
        assert r.status_code == 201
        body = r.json()
        assert body["entries"] == 1  # only Sol Ring resolved
        assert len(body["failures"]) == 1
        assert body["failures"][0]["name"] == "Unknown Card"

    def test_stream_endpoint_emits_events(self, client):
        # The SSE endpoint drives the UI's progress overlay. Parse the
        # response body as an event stream and check the important events
        # show up in the right order.
        with client.stream("POST", "/api/projects/stream", json={
            "name": "streamy", "decklist": "1 Sol Ring\n2 Duress\n",
        }) as resp:
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("text/event-stream")
            body = resp.read().decode()

        events = _parse_sse_body(body)
        names = [e["event"] for e in events]
        # Expected sequence for a plain decklist: start → phase → progress×N → done.
        assert names[0] == "start"
        assert events[0]["data"]["total"] == 2
        assert events[0]["data"]["name"] == "streamy"
        assert "phase" in names
        assert names.count("progress") == 2
        assert names[-1] == "done"
        assert events[-1]["data"]["entries"] == 2
        assert events[-1]["data"]["failures"] == []

    def test_stream_endpoint_streams_failures_at_the_end(self, client):
        with client.stream("POST", "/api/projects/stream", json={
            "name": "s2", "decklist": "1 Sol Ring\n1 Ghost Card\n",
        }) as resp:
            body = resp.read().decode()
        events = _parse_sse_body(body)
        done = events[-1]
        assert done["event"] == "done"
        assert done["data"]["entries"] == 1
        assert done["data"]["failures"][0]["name"] == "Ghost Card"

    def test_stream_endpoint_reports_moxfield_url_shape(self, client):
        # We can't hit real Moxfield in tests; verify the phase event fires
        # by seeing the moxfield-fetch phase attempted (it'll then error
        # because our FakeClient doesn't expose a Moxfield fetch — the
        # actual moxfield module is called and fails).
        with client.stream("POST", "/api/projects/stream", json={
            "name": "moxy",
            "decklist": "https://moxfield.com/decks/definitely-not-real",
        }) as resp:
            body = resp.read().decode()
        events = _parse_sse_body(body)
        phase_events = [e for e in events if e["event"] == "phase"]
        assert phase_events and phase_events[0]["data"]["phase"] == "moxfield-fetch"
        # The bogus deck ID makes the fetch fail — surface an error event.
        assert events[-1]["event"] == "error"


class TestProjectDelete:
    def test_deletes(self, seeded_client, tmp_path):
        r = seeded_client.delete("/api/projects/unit")
        assert r.status_code == 204
        assert not (tmp_path / "projects" / "unit.json").exists()

    def test_404_when_missing(self, client):
        r = client.delete("/api/projects/nope")
        assert r.status_code == 404


# --- Single project + entries -----------------------------------------------

class TestGetProject:
    def test_returns_full_json(self, seeded_client):
        r = seeded_client.get("/api/projects/unit")
        assert r.status_code == 200
        assert r.json()["name"] == "unit"
        assert len(r.json()["entries"]) == 2

    def test_404_when_missing(self, client):
        assert client.get("/api/projects/nope").status_code == 404


class TestScryfallSelectOnCustomEntry:
    """When a custom-art entry has its art swapped to a Scryfall printing,
    `custom_image_path` must be cleared and `oracle_id` filled in — otherwise
    the choice silently reverts on the next open of the project."""

    def test_select_clears_custom_image_path(self, client, tmp_path):
        # Hand-craft an entry that started as a library-only card.
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir(exist_ok=True)
        Project(
            name="swap",
            entries=[Entry(
                quantity=1, name="Sol Ring", oracle_id=ORACLE,
                selected_print=SelectedPrint(scryfall_id="", set="",
                                              collector_number=""),
                layout="normal", back="standard",
                custom_image_path="cache/images/custom/_library/prev.png",
            )],
        ).save(projects_dir)

        r = client.post("/api/projects/swap/select", json={
            "entry_index": 0, "scryfall_id": SR_ID,
        })
        assert r.status_code == 200
        assert r.json()["entry"]["custom_image_path"] in (None, "")

        # And the cleared value survives the round-trip to disk — this is
        # what the "art reverts on reopen" bug was violating.
        reloaded = Project.load("swap", projects_dir)
        assert reloaded.entries[0].custom_image_path in (None, "")
        assert reloaded.entries[0].selected_print.scryfall_id == SR_ID

    def test_select_backfills_missing_oracle_id(self, client, tmp_path):
        # A custom-art entry may have no oracle_id yet; picking a Scryfall
        # printing should populate it so the Printings tab keeps working
        # on subsequent modal opens.
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir(exist_ok=True)
        Project(
            name="no-oracle",
            entries=[Entry(
                quantity=1, name="Sol Ring", oracle_id="",
                selected_print=SelectedPrint(scryfall_id="", set="",
                                              collector_number=""),
                layout="normal", back="standard",
                custom_image_path="cache/images/custom/_library/x.png",
            )],
        ).save(projects_dir)

        # The endpoint needs to resolve scryfall_id via a direct card
        # lookup because there's no oracle_id to key the printings cache.
        r = client.post("/api/projects/no-oracle/select", json={
            "entry_index": 0, "scryfall_id": SR_ID,
        })
        assert r.status_code == 200
        reloaded = Project.load("no-oracle", projects_dir)
        assert reloaded.entries[0].oracle_id == ORACLE
        assert reloaded.entries[0].custom_image_path in (None, "")


class TestEntryCrud:
    def test_thumb_returns_url(self, seeded_client):
        r = seeded_client.get("/api/projects/unit/entries/0/thumb")
        assert r.status_code == 200
        assert r.json()["thumb_url"].endswith("sol-ring-c15.png")

    def test_select_updates_and_persists(self, seeded_client, tmp_path):
        r = seeded_client.post("/api/projects/unit/select",
                               json={"entry_index": 0, "scryfall_id": SR_ID_2})
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["entry"]["selected_print"]["set"] == "c21"

        # Persisted.
        reloaded = Project.load("unit", tmp_path / "projects")
        assert reloaded.entries[0].selected_print.scryfall_id == SR_ID_2

    def test_select_bad_index_is_400(self, seeded_client):
        r = seeded_client.post("/api/projects/unit/select",
                               json={"entry_index": 99, "scryfall_id": SR_ID})
        assert r.status_code == 400

    def test_delete_entry(self, seeded_client, tmp_path):
        r = seeded_client.delete("/api/projects/unit/entries/0")
        assert r.status_code == 204
        reloaded = Project.load("unit", tmp_path / "projects")
        assert len(reloaded.entries) == 1
        # The Duress entry (was index 1) shifted to index 0.
        assert reloaded.entries[0].name == "Duress"

    def test_delete_entry_bad_index_is_400(self, seeded_client):
        assert seeded_client.delete("/api/projects/unit/entries/99").status_code == 400


class TestScryfallSearch:
    def test_empty_query_returns_empty(self, client):
        r = client.get("/api/scryfall/search", params={"q": "  "})
        assert r.status_code == 200
        assert r.json() == {"results": [], "total_cards": 0}

    def test_finds_card_by_name(self, client):
        r = client.get("/api/scryfall/search", params={"q": "Sol Ring"})
        assert r.status_code == 200
        results = r.json()["results"]
        assert any(row["id"] == SR_ID for row in results)

    def test_token_kind_uses_token_filter(self, client):
        # FakeClient returns nothing for token queries; we're checking that
        # the endpoint routes the kind parameter through without error.
        r = client.get("/api/scryfall/search",
                       params={"q": "Zombie", "kind": "token"})
        assert r.status_code == 200
        assert r.json()["results"] == []

    def test_rejects_bad_kind(self, client):
        r = client.get("/api/scryfall/search",
                       params={"q": "Sol", "kind": "banana"})
        assert r.status_code == 400


class TestAddEntryFromScryfall:
    def test_appends_entry(self, seeded_client, tmp_path):
        r = seeded_client.post("/api/projects/unit/entries/from-scryfall",
                               json={"scryfall_id": SR_ID_2})
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["ok"] is True
        assert body["index"] == 2
        assert body["entry"]["name"] == "Sol Ring"
        assert body["entry"]["selected_print"]["scryfall_id"] == SR_ID_2

        reloaded = Project.load("unit", tmp_path / "projects")
        assert len(reloaded.entries) == 3
        assert reloaded.entries[-1].selected_print.scryfall_id == SR_ID_2

    def test_missing_card_is_404(self, seeded_client):
        r = seeded_client.post("/api/projects/unit/entries/from-scryfall",
                               json={"scryfall_id": "does-not-exist"})
        assert r.status_code == 404

    def test_bad_quantity_is_400(self, seeded_client):
        r = seeded_client.post("/api/projects/unit/entries/from-scryfall",
                               json={"scryfall_id": SR_ID, "quantity": 0})
        assert r.status_code == 400


class TestRegistrationTestEndpoint:
    def test_returns_pdf(self, client):
        r = client.get("/api/registration-test")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/pdf")
        # ReportLab always writes a %PDF- header at the start.
        assert r.content.startswith(b"%PDF-")

    def test_offsets_accepted(self, client):
        r = client.get("/api/registration-test",
                       params={"back_offset_x": "1.5", "back_offset_y": "-0.8"})
        assert r.status_code == 200

    def test_rejects_bad_flip_edge(self, client):
        r = client.get("/api/registration-test",
                       params={"flip_edge": "sideways"})
        assert r.status_code == 400




class TestNameValidation:
    @pytest.mark.parametrize("bad", [
        "",                     # empty
        "..",                   # path-traversal
        ".hidden",              # leading dot
        "-flag",                # leading dash (argparse-lookalike)
        "with/slash",           # path separator
        "with\\back",           # windows path separator
        "with:colon",           # windows-hostile
        'with"quote',           # windows-hostile
        "with|pipe",            # windows-hostile
        "with*star",            # windows-hostile
        "with?question",        # windows-hostile
        "a" * 100,              # too long
        "trip..dot",            # ".." anywhere
    ])
    def test_rejects_dangerous_names(self, client, bad):
        r = client.post("/api/projects", json={"name": bad,
                                                "decklist": "1 Sol Ring\n"})
        assert r.status_code == 400, f"expected 400 for {bad!r}"

    @pytest.mark.parametrize("good", [
        "deck",
        "My Deck",
        "test-2026",
        "under_score",
        # Commander names with commas, apostrophes, periods, ampersands.
        "Cass, Hand of Vengeance",
        "K'rrik, Son of Yawgmoth",
        "Mr. House, President and CEO",
        "Zurgo & Ojutai",
        "Feather, the Redeemed!",
        # Diacritics.
        "Sétya, Roiling Storm",
    ])
    def test_accepts_reasonable_names(self, client, good):
        r = client.post("/api/projects", json={"name": good,
                                                "decklist": "1 Sol Ring\n"})
        assert r.status_code == 201, f"expected 201 for {good!r}: {r.text}"


class TestSafeUploadName:
    """The upload sanitiser is a load-bearing security boundary: every
    caller that does `dir / _safe_upload_name(x)` relies on the return
    value being harmless."""

    @pytest.mark.parametrize("raw", [
        "normal.png",
        "a/b.png",                # path separator inside
        "a\\b.png",               # windows path separator inside
        "../evil.png",            # attempted traversal
        "...secret.png",          # leading dots
        "with<>bad|chars?.png",   # windows-hostile
        "\x00null.png",           # control character
    ])
    def test_output_is_always_path_safe(self, raw):
        got = srv._safe_upload_name(raw)
        # Whatever the sanitiser returns must never contain a path segment
        # separator, start with a dot (would hide the file / traverse up),
        # or contain characters the Windows FS rejects.
        assert "/" not in got
        assert "\\" not in got
        assert not got.startswith(".")
        assert not any(c in got for c in '<>:"|?*')
        assert not any(ord(c) < 0x20 for c in got)
        assert got, "sanitiser must never return empty"

    @pytest.mark.parametrize("reserved", [
        "CON.png", "con.png", "Con.PNG",     # case-insensitive
        "NUL.jpg", "PRN.png", "AUX.png",
        "COM1.png", "COM9.png", "LPT1.png", "LPT9.png",
    ])
    def test_windows_reserved_names_prefixed(self, reserved):
        got = srv._safe_upload_name(reserved)
        # Anything the OS would refuse to create must be prefixed so the
        # stem is no longer a reserved device name.
        stem = got.partition(".")[0]
        assert stem.upper() not in srv._WINDOWS_RESERVED_NAMES, got

    def test_never_empty(self):
        assert srv._safe_upload_name("") == "upload"
        assert srv._safe_upload_name("...") == "upload"
        assert srv._safe_upload_name("   ") == "upload"


class TestSlugifyProjectName:
    @pytest.mark.parametrize("raw,expected", [
        ("Mazirek Sacrifice", "mazirek_sacrifice"),
        ("Cass, Hand of Vengeance", "cass_hand_of_vengeance"),
        ("K'rrik, Son of Yawgmoth", "k_rrik_son_of_yawgmoth"),
        ("Mr. House, President and CEO", "mr_house_president_and_ceo"),
        ("Zurgo & Ojutai", "zurgo_ojutai"),
        ("Feather, the Redeemed!", "feather_the_redeemed"),
        ("test-2026", "test_2026"),
        # Diacritics collapse to underscore — not ideal but acceptable for
        # a folder name; the raw name is still displayed everywhere else.
        ("Sétya, Roiling Storm", "s_tya_roiling_storm"),
    ])
    def test_common_cases(self, raw, expected):
        assert srv._slugify_project_name(raw) == expected

    @pytest.mark.parametrize("raw", ["", "   ", "...", "…", "!!!"])
    def test_falls_back_when_no_alphanumerics(self, raw):
        assert srv._slugify_project_name(raw) == "project"
