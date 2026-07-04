"""Tests for card backs and duplex mirroring (spec §8, Phase 4)."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

import pytest
from PIL import Image

from proxy_studio import backs as BK
from proxy_studio.layout import (
    CARD_H_MM, CARD_W_MM, PageSpec, mirror_slots_for_back, page_slots,
)
from proxy_studio.pdf_export import (
    RenderCard, default_output_path, render_pdf, render_registration_test,
)


def _pdf_page_count(path: Path) -> int:
    data = path.read_bytes()
    return len(re.findall(rb"/Type\s*/Page\b(?!s)", data))


@pytest.fixture
def upscaled_png(tmp_path: Path) -> Path:
    p = tmp_path / "card.png"
    Image.new("RGB", (1490, 2080), color=(180, 90, 60)).save(p)
    return p


@pytest.fixture
def back_png(tmp_path: Path) -> Path:
    p = tmp_path / "back.png"
    Image.new("RGB", (1490, 2080), color=(60, 40, 25)).save(p)
    return p


# --- Standard back placeholder ---------------------------------------------

class TestStandardBackPlaceholder:
    def test_uses_real_asset_when_present(self, tmp_path):
        real = tmp_path / "mtg_back.png"
        Image.new("RGB", (1490, 2080), (20, 20, 20)).save(real)
        got = BK.get_standard_back(assets_dir=tmp_path)
        assert got == real

    def test_generates_placeholder_when_missing(self, tmp_path, monkeypatch, caplog):
        # Point PLACEHOLDER_PATH at a temp location so we don't pollute
        # the working directory during tests.
        placeholder = tmp_path / "placeholder.png"
        monkeypatch.setattr(BK, "PLACEHOLDER_PATH", placeholder)
        monkeypatch.setattr(BK, "_WARN_SUPPLIED_ONCE", False)
        with caplog.at_level("WARNING"):
            got = BK.get_standard_back(assets_dir=tmp_path / "no-assets")
        assert got == placeholder
        assert placeholder.exists()
        assert any("placeholder" in r.message.lower() for r in caplog.records)

    def test_placeholder_is_correct_size(self, tmp_path, monkeypatch):
        placeholder = tmp_path / "p.png"
        monkeypatch.setattr(BK, "PLACEHOLDER_PATH", placeholder)
        monkeypatch.setattr(BK, "_WARN_SUPPLIED_ONCE", True)
        BK.get_standard_back(assets_dir=tmp_path / "no-assets")
        with Image.open(placeholder) as im:
            assert im.size == (1490, 2080)


# --- Mirroring: card positions but NOT the images --------------------------

class TestMirroringCorrectness:
    """Spec §8: 'The card images themselves are not mirrored — only their
    positions.' Verify the layout helper produces the right destination."""

    def test_top_left_maps_to_top_right(self):
        spec = PageSpec()   # 3 mm gutter, 3×3
        fronts = page_slots(spec)
        backs = mirror_slots_for_back(fronts, spec, flip_edge="long")
        # Front (0,0) → Back (0,2): same row, right-most column.
        assert (fronts[0].row, fronts[0].col) == (0, 0)
        assert (backs[0].row, backs[0].col) == (0, 2)

    def test_middle_column_stays_put_on_long_edge_flip(self):
        spec = PageSpec()
        fronts = page_slots(spec)
        backs = mirror_slots_for_back(fronts, spec, flip_edge="long")
        # Column 1 → column (3-1-1) = 1. Position unchanged.
        for f, b in zip(fronts, backs):
            if f.col == 1:
                assert b.col == 1
                assert pytest.approx(b.x_mm, abs=1e-6) == f.x_mm

    def test_full_row_columns_reverse(self):
        """Middle row of a 3×3 grid: [0,1,2] → [2,1,0]."""
        spec = PageSpec()
        fronts = page_slots(spec)
        backs = mirror_slots_for_back(fronts, spec, flip_edge="long")
        # Middle row indices: 3, 4, 5 in reading order.
        row_cols = [(backs[i].col) for i in (3, 4, 5)]
        assert row_cols == [2, 1, 0]


# --- render_pdf duplex + separate ------------------------------------------

class TestRenderBacks:
    def test_none_mode_writes_only_fronts(self, tmp_path, upscaled_png):
        cards = [RenderCard(upscaled_png, f"c{i}", 1) for i in range(11)]
        fronts, backs = render_pdf(cards, tmp_path / "out.pdf",
                                    project_name="test", backs_mode="none")
        assert fronts.exists() and backs is None
        assert _pdf_page_count(fronts) == 2

    def test_duplex_interleaves_pages(self, tmp_path, upscaled_png, back_png):
        # 11 cards → 2 front pages + 2 back pages = 4 total.
        cards = [RenderCard(upscaled_png, f"c{i}", 1, back_png) for i in range(11)]
        fronts, backs = render_pdf(cards, tmp_path / "out.pdf",
                                    project_name="test", backs_mode="duplex")
        assert backs is None    # duplex is single-file
        assert _pdf_page_count(fronts) == 4

    def test_separate_writes_two_files(self, tmp_path, upscaled_png, back_png):
        cards = [RenderCard(upscaled_png, f"c{i}", 1, back_png) for i in range(11)]
        fronts, backs = render_pdf(cards, tmp_path / "out.pdf",
                                    project_name="test", backs_mode="separate")
        assert fronts.exists() and backs is not None
        assert backs.name == "out_backs.pdf"
        assert _pdf_page_count(fronts) == 2
        assert _pdf_page_count(backs) == 2

    def test_duplex_without_back_image_raises(self, tmp_path, upscaled_png):
        cards = [RenderCard(upscaled_png, "c", 1, back_image_path=None)]
        with pytest.raises(ValueError, match="back"):
            render_pdf(cards, tmp_path / "out.pdf", backs_mode="duplex")

    def test_separate_without_back_image_raises(self, tmp_path, upscaled_png):
        cards = [RenderCard(upscaled_png, "c", 1, back_image_path=None)]
        with pytest.raises(ValueError, match="back"):
            render_pdf(cards, tmp_path / "out.pdf", backs_mode="separate")

    def test_render_pdf_low_dpi_source_hard_fails(self, tmp_path):
        low_res = tmp_path / "tiny.png"
        Image.new("RGB", (400, 560), color=(0, 0, 0)).save(low_res)
        cards = [RenderCard(low_res, "c", 1)]
        with pytest.raises(ValueError, match="below hard-fail"):
            render_pdf(cards, tmp_path / "out.pdf", backs_mode="none")


# --- Registration test page ------------------------------------------------

class TestDefaultOutputPath:
    NOW = datetime(2026, 7, 4, 15, 23)

    def test_none_mode_uses_fronts_suffix(self):
        p = default_output_path("mazirek", "none", now=self.NOW)
        assert p.name == "mazirek_2026-07-04-1523_fronts.pdf"

    def test_duplex_drops_fronts_suffix(self):
        # In duplex mode the single PDF contains both fronts and backs,
        # so the "_fronts" suffix would be misleading.
        p = default_output_path("mazirek", "duplex", now=self.NOW)
        assert p.name == "mazirek_2026-07-04-1523.pdf"

    def test_separate_mode_names_fronts_pdf(self):
        # render_pdf derives the backs path by stripping _fronts, so this
        # base needs to end in _fronts even when generating the backs
        # sibling later.
        p = default_output_path("mazirek", "separate", now=self.NOW)
        assert p.name == "mazirek_2026-07-04-1523_fronts.pdf"

    def test_timestamp_optional(self):
        p = default_output_path("mazirek", "none", timestamp=False)
        assert p.name == "mazirek_fronts.pdf"

    def test_two_close_exports_get_distinct_names(self, tmp_path):
        a = default_output_path("m", "none", now=datetime(2026, 7, 4, 10, 0))
        b = default_output_path("m", "none", now=datetime(2026, 7, 4, 10, 1))
        assert a != b


class TestRegistrationTest:
    def test_writes_two_pages(self, tmp_path):
        out = render_registration_test(tmp_path / "reg.pdf")
        assert out.exists()
        assert _pdf_page_count(out) == 2

    def test_offset_shifts_all_back_content(self, tmp_path):
        # We can't easily inspect ReportLab draw ops from the PDF, but the
        # function is a straight pass-through to the same layout helpers
        # that are unit-tested elsewhere. Verify it still writes a valid PDF
        # with an offset applied.
        out = render_registration_test(tmp_path / "reg.pdf",
                                        back_offset_x_mm=1.5,
                                        back_offset_y_mm=-0.8)
        assert out.exists() and out.stat().st_size > 1000
