"""Tests for the bring-your-own-art flow (uploads + reorder + custom entries)."""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from proxy_studio import scryfall as SF
from proxy_studio import server as srv
from proxy_studio.project import Entry, Project, SelectedPrint


class FakeClient:
    """The `create_project` path doesn't hit Scryfall for empty decklists,
    but a client is required by the AppState — provide a no-op."""

    def resolve_named(self, name, set_code=None, collector_number=None):
        raise SF.NotFoundError(name)

    def _get_json(self, url):
        return {"__http_status": 404}


@pytest.fixture
def client(tmp_path, monkeypatch):
    # Point the upload + backs roots at a temp dir so tests don't touch the
    # real cache.
    monkeypatch.setattr(srv, "UPLOADS_ROOT", tmp_path / "uploads")
    monkeypatch.setattr(srv, "BACKS_ROOT", tmp_path / "backs")
    (tmp_path / "uploads").mkdir()
    (tmp_path / "backs").mkdir()
    app = srv.create_app(projects_dir=tmp_path / "projects",
                         cache_dir=tmp_path / "cache")
    app.state.picker.client = FakeClient()
    return TestClient(app)


def _png_bytes(color=(255, 0, 0), size=(64, 64)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


class TestEmptyProject:
    def test_create_with_no_decklist_no_files(self, client):
        r = client.post("/api/projects", json={"name": "empty", "decklist": ""})
        assert r.status_code == 201
        assert r.json()["entries"] == 0

    def test_get_empty_project(self, client):
        client.post("/api/projects", json={"name": "empty", "decklist": ""})
        r = client.get("/api/projects/empty")
        assert r.status_code == 200
        assert r.json()["entries"] == []


class TestUploads:
    def test_upload_creates_entries(self, client):
        client.post("/api/projects", json={"name": "art", "decklist": ""})
        files = [
            ("files", ("sol_ring.png", _png_bytes(color=(200, 100, 50)), "image/png")),
            ("files", ("duress.jpg", _png_bytes(color=(20, 20, 20)), "image/jpeg")),
        ]
        r = client.post("/api/projects/art/uploads", files=files)
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["entries"] == 2
        names = [a["name"] for a in body["added"]]
        # Default entry name is derived from the filename stem.
        assert "Sol Ring" in names
        assert "Duress" in names

    def test_upload_writes_file_to_library(self, client, tmp_path):
        client.post("/api/projects", json={"name": "art", "decklist": ""})
        client.post("/api/projects/art/uploads", files=[
            ("files", ("card.png", _png_bytes(), "image/png")),
        ])
        # Uploads now land in the shared library, not a per-project dir.
        assert (tmp_path / "uploads" / "_library" / "card.png").exists()

    def test_upload_rejects_unsupported_extension(self, client):
        client.post("/api/projects", json={"name": "art", "decklist": ""})
        r = client.post("/api/projects/art/uploads", files=[
            ("files", ("bad.zip", b"not-an-image", "application/zip")),
        ])
        assert r.status_code == 400

    def test_upload_uniquifies_filename_on_collision(self, client, tmp_path):
        client.post("/api/projects", json={"name": "art", "decklist": ""})
        files = [("files", ("card.png", _png_bytes(), "image/png"))]
        client.post("/api/projects/art/uploads", files=files)
        client.post("/api/projects/art/uploads", files=[
            ("files", ("card.png", _png_bytes(color=(0,255,0)), "image/png")),
        ])
        d = tmp_path / "uploads" / "_library"
        names = {p.name for p in d.iterdir()}
        assert "card.png" in names
        assert "card-2.png" in names

    def test_delete_project_preserves_library(self, client, tmp_path):
        # Library uploads survive project deletion — that's the whole point
        # of a shared asset library.
        client.post("/api/projects", json={"name": "art", "decklist": ""})
        client.post("/api/projects/art/uploads", files=[
            ("files", ("card.png", _png_bytes(), "image/png")),
        ])
        library_file = tmp_path / "uploads" / "_library" / "card.png"
        assert library_file.exists()

        r = client.delete("/api/projects/art")
        assert r.status_code == 204
        assert library_file.exists()   # still there


class TestReorder:
    @pytest.fixture
    def three_card_project(self, client, tmp_path):
        client.post("/api/projects", json={"name": "trio", "decklist": ""})
        client.post("/api/projects/trio/uploads", files=[
            ("files", ("alpha.png", _png_bytes(color=(10, 10, 10)), "image/png")),
            ("files", ("beta.png", _png_bytes(color=(20, 20, 20)), "image/png")),
            ("files", ("gamma.png", _png_bytes(color=(30, 30, 30)), "image/png")),
        ])
        return "trio"

    def test_reorder_updates_order(self, client, three_card_project):
        # Reverse the order.
        r = client.put(f"/api/projects/{three_card_project}/order",
                       json={"order": [2, 1, 0]})
        assert r.status_code == 200
        got = [e["name"] for e in client.get(f"/api/projects/{three_card_project}").json()["entries"]]
        assert got == ["Gamma", "Beta", "Alpha"]

    def test_reorder_rejects_bad_permutation(self, client, three_card_project):
        r = client.put(f"/api/projects/{three_card_project}/order",
                       json={"order": [0, 0, 1]})
        assert r.status_code == 400

    def test_reorder_rejects_wrong_length(self, client, three_card_project):
        r = client.put(f"/api/projects/{three_card_project}/order",
                       json={"order": [0, 1]})
        assert r.status_code == 400


class TestCustomEntryPersistence:
    def test_survives_save_load_roundtrip(self, tmp_path):
        p = Project(
            name="rt",
            entries=[Entry(
                quantity=1, name="Custom",
                oracle_id="",
                selected_print=SelectedPrint(scryfall_id="", set="", collector_number=""),
                layout="normal", back="standard",
                custom_image_path="cache/images/custom/rt/card.png",
            )],
        )
        p.save(tmp_path)
        loaded = Project.load("rt", tmp_path)
        assert loaded.entries[0].custom_image_path == "cache/images/custom/rt/card.png"


class TestDuplicateEntry:
    def test_duplicate_inserts_copy_after(self, client, tmp_path):
        client.post("/api/projects", json={"name": "dup", "decklist": ""})
        client.post("/api/projects/dup/uploads", files=[
            ("files", ("alpha.png", _png_bytes(color=(10, 10, 10)), "image/png")),
            ("files", ("beta.png",  _png_bytes(color=(20, 20, 20)), "image/png")),
        ])
        r = client.post("/api/projects/dup/entries/0/duplicate")
        assert r.status_code == 201
        assert r.json()["entries"] == 3
        names = [e["name"] for e in client.get("/api/projects/dup").json()["entries"]]
        # Alpha, Alpha (copy at index 1), Beta.
        assert names == ["Alpha", "Alpha", "Beta"]

    def test_duplicate_preserves_custom_image_path(self, client, tmp_path):
        client.post("/api/projects", json={"name": "dup2", "decklist": ""})
        client.post("/api/projects/dup2/uploads", files=[
            ("files", ("card.png", _png_bytes(), "image/png")),
        ])
        client.post("/api/projects/dup2/entries/0/duplicate")
        entries = client.get("/api/projects/dup2").json()["entries"]
        # Both entries share the same underlying file — no filesystem dupe.
        assert entries[0]["custom_image_path"] == entries[1]["custom_image_path"]

    def test_duplicate_bad_index_is_400(self, client):
        client.post("/api/projects", json={"name": "d3", "decklist": ""})
        r = client.post("/api/projects/d3/entries/99/duplicate")
        assert r.status_code == 400


class TestLibraryEndpoints:
    def test_upload_to_library_creates_no_entries(self, client, tmp_path):
        # Uploading to the library shouldn't touch any project.
        client.post("/api/projects", json={"name": "somewhere", "decklist": ""})
        r = client.post("/api/library/uploads", files=[
            ("files", ("shared.png", _png_bytes(), "image/png")),
        ])
        assert r.status_code == 201
        assert (tmp_path / "uploads" / "_library" / "shared.png").exists()
        proj = client.get("/api/projects/somewhere").json()
        assert proj["entries"] == []

    def test_list_library_returns_assets(self, client, tmp_path):
        client.post("/api/library/uploads", files=[
            ("files", ("a.png", _png_bytes(), "image/png")),
            ("files", ("b.png", _png_bytes(color=(0, 200, 0)), "image/png")),
        ])
        r = client.get("/api/library")
        assert r.status_code == 200
        names = sorted(a["filename"] for a in r.json())
        assert names == ["a.png", "b.png"]

    def test_add_from_library_creates_entries(self, client, tmp_path):
        client.post("/api/projects", json={"name": "reuser", "decklist": ""})
        client.post("/api/library/uploads", files=[
            ("files", ("land.png", _png_bytes(), "image/png")),
        ])
        r = client.post("/api/projects/reuser/entries/from-library",
                        json={"filenames": ["land.png"]})
        assert r.status_code == 201
        proj = client.get("/api/projects/reuser").json()
        assert len(proj["entries"]) == 1
        assert proj["entries"][0]["custom_image_path"].endswith("/_library/land.png")

    def test_add_from_library_multiple_shares_one_file(self, client, tmp_path):
        # Two decks reference the same underlying library file.
        client.post("/api/library/uploads", files=[
            ("files", ("shared.png", _png_bytes(), "image/png")),
        ])
        for deck in ("deck-one", "deck-two"):
            client.post("/api/projects", json={"name": deck, "decklist": ""})
            client.post(f"/api/projects/{deck}/entries/from-library",
                        json={"filenames": ["shared.png"]})
        files_in_lib = list((tmp_path / "uploads" / "_library").iterdir())
        assert [p.name for p in files_in_lib] == ["shared.png"]

    def test_add_from_library_missing_file_is_404(self, client):
        client.post("/api/projects", json={"name": "p", "decklist": ""})
        r = client.post("/api/projects/p/entries/from-library",
                        json={"filenames": ["ghost.png"]})
        assert r.status_code == 404

    def test_delete_library_asset(self, client, tmp_path):
        client.post("/api/library/uploads", files=[
            ("files", ("gone.png", _png_bytes(), "image/png")),
        ])
        r = client.delete("/api/library/gone.png")
        assert r.status_code == 204
        assert not (tmp_path / "uploads" / "_library" / "gone.png").exists()

    def test_delete_library_missing_asset_is_404(self, client):
        r = client.delete("/api/library/nope.png")
        assert r.status_code == 404


class TestSelectLibraryForEntry:
    """Swap an existing entry's art to a library asset (the modal-Library-tab
    flow). Preserves entry name + quantity + selected_print; only mutates
    custom_image_path."""

    def test_swap_scryfall_entry_to_library(self, client, tmp_path):
        # Create a project with a Scryfall-looking entry hand-crafted on disk
        # (avoids needing the full Scryfall fake), then swap its art.
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir(exist_ok=True)
        Project(
            name="deck",
            entries=[Entry(
                quantity=1, name="Mountain",
                oracle_id="oracle-mtn",
                selected_print=SelectedPrint(scryfall_id="print-mtn-1",
                                              set="m21", collector_number="123"),
                layout="normal", back="standard",
            )],
        ).save(projects_dir)
        client.post("/api/library/uploads", files=[
            ("files", ("custom_mountain.png", _png_bytes(), "image/png")),
        ])

        r = client.post("/api/projects/deck/entries/0/select-library",
                        json={"filename": "custom_mountain.png"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["entry"]["custom_image_path"].endswith("/_library/custom_mountain.png")
        assert body["entry"]["thumb_url"].endswith("/_library/custom_mountain.png")

        # Persisted: selected_print stays around (so a "revert to printing"
        # feature could restore it), but custom_image_path now overrides.
        reloaded = Project.load("deck", projects_dir)
        assert reloaded.entries[0].custom_image_path.endswith("/_library/custom_mountain.png")
        assert reloaded.entries[0].selected_print.scryfall_id == "print-mtn-1"
        assert reloaded.entries[0].name == "Mountain"
        assert reloaded.entries[0].quantity == 1

    def test_swap_library_to_library(self, client, tmp_path):
        client.post("/api/projects", json={"name": "d", "decklist": ""})
        client.post("/api/library/uploads", files=[
            ("files", ("first.png",  _png_bytes(), "image/png")),
            ("files", ("second.png", _png_bytes(color=(0,200,0)), "image/png")),
        ])
        client.post("/api/projects/d/entries/from-library",
                    json={"filenames": ["first.png"]})
        # Now swap the entry's art to `second.png`.
        r = client.post("/api/projects/d/entries/0/select-library",
                        json={"filename": "second.png"})
        assert r.status_code == 200
        entries = client.get("/api/projects/d").json()["entries"]
        assert entries[0]["custom_image_path"].endswith("/_library/second.png")

    def test_swap_missing_asset_is_404(self, client):
        client.post("/api/projects", json={"name": "d2", "decklist": ""})
        client.post("/api/library/uploads", files=[
            ("files", ("real.png", _png_bytes(), "image/png")),
        ])
        client.post("/api/projects/d2/entries/from-library",
                    json={"filenames": ["real.png"]})
        r = client.post("/api/projects/d2/entries/0/select-library",
                        json={"filename": "ghost.png"})
        assert r.status_code == 404

    def test_swap_bad_index_is_400(self, client):
        client.post("/api/projects", json={"name": "d3", "decklist": ""})
        client.post("/api/library/uploads", files=[
            ("files", ("x.png", _png_bytes(), "image/png")),
        ])
        r = client.post("/api/projects/d3/entries/99/select-library",
                        json={"filename": "x.png"})
        assert r.status_code == 400


class TestBacksLibrary:
    def test_upload_lands_in_backs_dir(self, client, tmp_path):
        r = client.post("/api/backs/uploads", files=[
            ("files", ("plain_black.png", _png_bytes(), "image/png")),
        ])
        assert r.status_code == 201
        assert (tmp_path / "backs" / "plain_black.png").exists()

    def test_list_backs(self, client):
        client.post("/api/backs/uploads", files=[
            ("files", ("one.png", _png_bytes(), "image/png")),
            ("files", ("two.png", _png_bytes(color=(0,200,0)), "image/png")),
        ])
        r = client.get("/api/backs")
        assert r.status_code == 200
        names = sorted(a["filename"] for a in r.json())
        assert names == ["one.png", "two.png"]

    def test_delete_back(self, client, tmp_path):
        client.post("/api/backs/uploads", files=[
            ("files", ("gone.png", _png_bytes(), "image/png")),
        ])
        r = client.delete("/api/backs/gone.png")
        assert r.status_code == 204
        assert not (tmp_path / "backs" / "gone.png").exists()

    def test_set_project_default_back(self, client, tmp_path):
        client.post("/api/projects", json={"name": "p", "decklist": ""})
        client.post("/api/backs/uploads", files=[
            ("files", ("brown.png", _png_bytes(), "image/png")),
        ])
        r = client.post("/api/projects/p/default-back",
                        json={"filename": "brown.png"})
        assert r.status_code == 200
        assert r.json()["default_back_filename"] == "brown.png"

        # Round-trip through disk.
        proj = Project.load("p", tmp_path / "projects")
        assert proj.default_back_filename == "brown.png"

    def test_set_default_back_rejects_missing_file(self, client):
        client.post("/api/projects", json={"name": "p", "decklist": ""})
        r = client.post("/api/projects/p/default-back",
                        json={"filename": "ghost.png"})
        assert r.status_code == 404

    def test_clear_default_back(self, client, tmp_path):
        client.post("/api/projects", json={"name": "p", "decklist": ""})
        client.post("/api/backs/uploads", files=[
            ("files", ("x.png", _png_bytes(), "image/png")),
        ])
        client.post("/api/projects/p/default-back", json={"filename": "x.png"})
        r = client.post("/api/projects/p/default-back", json={"filename": None})
        assert r.status_code == 200
        proj = Project.load("p", tmp_path / "projects")
        assert proj.default_back_filename is None


class TestEntryThumbSurface:
    def test_custom_entry_thumb_url(self, client, tmp_path):
        client.post("/api/projects", json={"name": "art", "decklist": ""})
        client.post("/api/projects/art/uploads", files=[
            ("files", ("card.png", _png_bytes(), "image/png")),
        ])
        r = client.get("/api/projects/art/entries/0/thumb")
        assert r.status_code == 200
        body = r.json()
        # After the library refactor, thumbs live under /uploads/_library/.
        assert body["thumb_url"].endswith("/_library/card.png")
        assert body["custom_image_path"] == "cache/images/custom/_library/card.png"
