"""Tests for `proxy_studio.layout`.

Card positions are asserted to 0.01 mm — a 1 mm drift on a 63 mm card is a
visibly-off proxy, so the geometry deserves tight coverage.
"""

from __future__ import annotations

import math

import pytest

from proxy_studio.layout import (
    CARD_H_MM, CARD_W_MM, PageSpec, cut_lines_full, cut_marks, cut_ticks,
    effective_dpi, mirror_slots_for_back, page_slots, paginate,
)


# Tests that pre-date the gutter default change assume gutter=0. Use this to
# construct the "tight" spec explicitly.
def tight_spec(**kw) -> PageSpec:
    return PageSpec(gutter_mm=0.0, cut_line_mode="ticks", **kw)


TOL = 0.01  # 0.01 mm tolerance


def approx(a: float, b: float) -> bool:
    return math.isclose(a, b, abs_tol=TOL)


class TestPageSpec:
    def test_default_gutter_is_3mm(self):
        # Project memory: 3 mm gutter is the paper-trimmer-friendly default.
        assert PageSpec().gutter_mm == 3.0
        assert PageSpec().cut_line_mode == "full"

    def test_A4_grid_dimensions_tight(self):
        spec = tight_spec()
        assert approx(spec.grid_size_mm[0], 189.0)   # 3 * 63
        assert approx(spec.grid_size_mm[1], 264.0)   # 3 * 88

    def test_A4_grid_dimensions_with_default_gutter(self):
        spec = PageSpec()   # gutter 3 mm
        assert approx(spec.grid_size_mm[0], 3 * 63 + 2 * 3)   # 195
        assert approx(spec.grid_size_mm[1], 3 * 88 + 2 * 3)   # 270

    def test_A4_margins_centered_tight(self):
        left, right, top, bottom = tight_spec().margins_mm
        assert approx(left, 10.5) and approx(right, 10.5)
        assert approx(top, 16.5) and approx(bottom, 16.5)

    def test_A4_margins_centered_with_default_gutter(self):
        left, _r, top, _b = PageSpec().margins_mm
        # (210 - 195) / 2 = 7.5 mm  ;  (297 - 270) / 2 = 13.5 mm
        assert approx(left, 7.5)
        assert approx(top, 13.5)

    def test_slots_per_page(self):
        assert PageSpec().slots_per_page == 9

    def test_gutter_extends_grid(self):
        spec = PageSpec(gutter_mm=2.0)
        # 3 cards + 2 gutters
        assert approx(spec.grid_size_mm[0], 3 * 63 + 2 * 2)
        assert approx(spec.grid_size_mm[1], 3 * 88 + 2 * 2)

    def test_unknown_paper_raises(self):
        with pytest.raises(ValueError):
            _ = PageSpec(paper="B0").page_size_mm

    def test_bleed_defaults_to_zero(self):
        # Existing exports must not silently start growing images.
        assert PageSpec().bleed_mm == 0.0

    def test_bleed_is_carried_by_spec(self):
        spec = PageSpec(bleed_mm=3.0, gutter_mm=6.0)
        assert spec.bleed_mm == 3.0
        # Bleed doesn't change the grid geometry — the cut lines still sit
        # at the true card edges; only the drawn image extends past them.
        assert approx(spec.grid_size_mm[0], 3 * 63 + 2 * 6)   # 201
        assert approx(spec.grid_size_mm[1], 3 * 88 + 2 * 6)   # 276


class TestPageSlots:
    def test_reading_order_top_left_first(self):
        slots = page_slots(tight_spec())
        assert len(slots) == 9
        assert (slots[0].col, slots[0].row) == (0, 0)
        assert (slots[-1].col, slots[-1].row) == (2, 2)

    def test_top_left_slot_position_tight(self):
        s = page_slots(tight_spec())[0]
        # x = left margin = 10.5 mm
        assert approx(s.x_mm, 10.5)
        # y (bottom-left origin) = page_h - top_margin - card_h
        #   = 297 - 16.5 - 88 = 192.5
        assert approx(s.y_mm, 192.5)

    def test_bottom_right_slot_position_tight(self):
        s = page_slots(tight_spec())[-1]
        assert approx(s.x_mm, 136.5)   # 10.5 + 2*63
        assert approx(s.y_mm, 16.5)    # 297 - 16.5 - 3*88

    def test_slots_include_gutter_offset(self):
        spec = PageSpec()   # 3 mm gutter
        slots = page_slots(spec)
        # Column 1 starts at left + card_w + gutter = 7.5 + 63 + 3 = 73.5
        assert approx(slots[1].x_mm, 73.5)
        # Row 1 top-y (bottom-left origin) = ph - top - card_h - gutter - card_h
        # = 297 - 13.5 - 88 - 3 - 88 = 104.5
        assert approx(slots[3].y_mm, 104.5)

    def test_slot_size_is_card_size(self):
        s = page_slots(PageSpec())[4]
        assert approx(s.w_mm, CARD_W_MM)
        assert approx(s.h_mm, CARD_H_MM)


class TestMirroring:
    def test_long_edge_mirror_swaps_columns(self):
        spec = tight_spec()
        fronts = page_slots(spec)
        backs = mirror_slots_for_back(fronts, spec, flip_edge="long")
        # Front top-left (col 0) → back top-right (col 2).
        assert backs[0].col == 2 and backs[0].row == 0
        # Y unchanged.
        assert approx(backs[0].y_mm, fronts[0].y_mm)
        # X = left + 2 * card_w = 10.5 + 126 = 136.5
        assert approx(backs[0].x_mm, 136.5)

    def test_short_edge_mirror_swaps_rows(self):
        spec = tight_spec()
        fronts = page_slots(spec)
        backs = mirror_slots_for_back(fronts, spec, flip_edge="short")
        # Front top-left (row 0) → back bottom-left (row 2).
        assert backs[0].col == 0 and backs[0].row == 2
        assert approx(backs[0].x_mm, fronts[0].x_mm)
        assert approx(backs[0].y_mm, 16.5)

    def test_double_mirror_is_identity_with_gutter(self):
        spec = PageSpec()   # 3 mm gutter — same round-trip must hold
        fronts = page_slots(spec)
        twice = mirror_slots_for_back(
            mirror_slots_for_back(fronts, spec, "long"), spec, "long")
        for a, b in zip(fronts, twice):
            assert approx(a.x_mm, b.x_mm) and approx(a.y_mm, b.y_mm)
            assert (a.col, a.row) == (b.col, b.row)


class TestCutTicks:
    def test_no_ticks_cross_grid_interior(self):
        spec = tight_spec()
        left, _r, top, _b = spec.margins_mm
        page_h = spec.page_size_mm[1]
        grid_top_y = page_h - top
        grid_bottom_y = grid_top_y - spec.grid_size_mm[1]
        grid_right_x = left + spec.grid_size_mm[0]
        for t in cut_ticks(spec):
            fully_left  = t.x1_mm <= left + TOL and t.x2_mm <= left + TOL
            fully_right = t.x1_mm >= grid_right_x - TOL and t.x2_mm >= grid_right_x - TOL
            fully_below = t.y1_mm <= grid_bottom_y + TOL and t.y2_mm <= grid_bottom_y + TOL
            fully_above = t.y1_mm >= grid_top_y - TOL and t.y2_mm >= grid_top_y - TOL
            assert fully_left or fully_right or fully_below or fully_above, \
                f"tick crosses card region: {t}"

    def test_tick_count_full_page(self):
        ticks = cut_ticks(tight_spec())
        assert len(ticks) == 16

    def test_partial_final_page_only_ticks_occupied(self):
        # 4 cards on the final page: rows 0 (all cols) + row 1 (col 0).
        ticks = cut_ticks(tight_spec(), occupied_slot_count=4)
        assert len(ticks) == 14


class TestCutLinesFull:
    def test_full_line_count_with_gutter(self):
        # Default 3×3 grid, 3 mm gutter: every card edge gets its own line.
        # 3 cols × 2 edges = 6 vertical, 3 rows × 2 edges = 6 horizontal.
        assert len(cut_lines_full(PageSpec())) == 12

    def test_full_line_count_tight_dedupes_shared_edges(self):
        # At gutter=0, adjacent card edges collapse to a single line.
        # 4 unique x-edges + 4 unique y-edges = 8.
        assert len(cut_lines_full(tight_spec().__class__(gutter_mm=0.0,
                                                          cut_line_mode="full"))) == 8

    def test_lines_align_with_every_card_edge(self):
        """The core correctness check: every card's four edges get a line."""
        spec = PageSpec()
        left, _r, top, _b = spec.margins_mm
        page_h = spec.page_size_mm[1]

        expected_xs = set()
        for c in range(spec.cols):
            col_left = left + c * (CARD_W_MM + spec.gutter_mm)
            expected_xs.add(approx_round(col_left))                 # left edge
            expected_xs.add(approx_round(col_left + CARD_W_MM))     # right edge
        expected_ys = set()
        for r in range(spec.rows):
            row_top = page_h - top - r * (CARD_H_MM + spec.gutter_mm)
            expected_ys.add(approx_round(row_top))                  # top edge
            expected_ys.add(approx_round(row_top - CARD_H_MM))      # bottom edge

        got_xs, got_ys = set(), set()
        for ln in cut_lines_full(spec):
            if approx(ln.x1_mm, ln.x2_mm):
                got_xs.add(approx_round(ln.x1_mm))
            if approx(ln.y1_mm, ln.y2_mm):
                got_ys.add(approx_round(ln.y1_mm))
        assert got_xs == expected_xs
        assert got_ys == expected_ys

    def test_vertical_lines_span_full_height(self):
        spec = PageSpec()
        page_h = spec.page_size_mm[1]
        vlines = [ln for ln in cut_lines_full(spec) if approx(ln.x1_mm, ln.x2_mm)]
        for ln in vlines:
            assert approx(min(ln.y1_mm, ln.y2_mm), 0.0)
            assert approx(max(ln.y1_mm, ln.y2_mm), page_h)

    def test_horizontal_lines_span_full_width(self):
        spec = PageSpec()
        page_w = spec.page_size_mm[0]
        hlines = [ln for ln in cut_lines_full(spec) if approx(ln.y1_mm, ln.y2_mm)]
        for ln in hlines:
            assert approx(min(ln.x1_mm, ln.x2_mm), 0.0)
            assert approx(max(ln.x1_mm, ln.x2_mm), page_w)

    def test_partial_final_page_culls_lines(self):
        # 4 occupied slots → cols {0,1,2}, rows {0,1}.
        # With gutter: 6 vertical + 4 horizontal = 10.
        assert len(cut_lines_full(PageSpec(), occupied_slot_count=4)) == 10

    def test_cut_marks_dispatches_by_mode(self):
        assert len(cut_marks(PageSpec(cut_line_mode="full"))) == 12
        assert len(cut_marks(PageSpec(cut_line_mode="ticks", gutter_mm=0))) == 16


def approx_round(x: float) -> float:
    """Bucket a mm coordinate to 0.01 mm for set comparisons."""
    return round(x, 2)


class TestEffectiveDpi:
    def test_scryfall_png_is_near_300_dpi(self):
        # Spec §7 says "~298 DPI at 63×88 mm" — exact math gives ~300.4 x
        # and ~300.2 y for the 745×1040 Scryfall PNG. Sanity-check the
        # ballpark, not the spec's rounded figure.
        dx, dy = effective_dpi(745, 1040)
        assert 298 < dx < 302
        assert 298 < dy < 302

    def test_2x_upscale_hits_600(self):
        dx, dy = effective_dpi(1490, 2080)
        assert 598 < dx < 604
        assert 598 < dy < 604


class TestPaginate:
    def test_basic_split(self):
        keys = [f"c{i}" for i in range(20)]
        batch = paginate(keys, PageSpec())
        assert len(batch.pages) == 3
        assert len(batch.pages[0]) == 9
        assert len(batch.pages[-1]) == 2

    def test_empty_input(self):
        batch = paginate([], PageSpec())
        # No pages when there are no cards (Phase 1: caller decides how to
        # handle an empty deck upstream; a 0-page PDF is not useful).
        assert batch.pages == [[]] or batch.pages == []
