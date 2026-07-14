"""Tests for `corner_fill_image` — filling the rounded-corner die-cut so a
physical corner-rounder can't expose a white/transparent sliver."""

from __future__ import annotations

import pytest
from PIL import Image, ImageDraw

from proxy_studio import pdf_export as P


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch):
    """Route the corner-fill cache into a tmp dir so tests don't touch the
    real `cache/images/corner/`."""
    monkeypatch.setattr(P, "CORNER_FILL_CACHE_DIR", tmp_path / "corner")


def _rgba_with_transparent_corners(size=(120, 168), radius=8) -> Image.Image:
    """A rounded-rect opaque card on a transparent background — mimics a
    Scryfall PNG's die-cut."""
    im = Image.new("RGBA", size, (0, 0, 0, 0))
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, size[0] - 1, size[1] - 1), radius=radius, fill=255)
    card = Image.new("RGBA", size, (200, 30, 30, 255))  # red card face
    im.paste(card, (0, 0), mask)
    return im


def test_alpha_composite_fills_transparent_corner_black(tmp_path):
    src = tmp_path / "card.png"
    _rgba_with_transparent_corners().save(src)
    assert Image.open(src).getpixel((0, 0))[3] == 0  # transparent corner

    out = P.corner_fill_image(src, (0.0, 0.0, 0.0))
    res = Image.open(out)
    assert res.mode == "RGB"                     # flattened, no alpha
    assert res.getpixel((0, 0)) == (0, 0, 0)     # corner is now solid black


def test_alpha_composite_preserves_card_face(tmp_path):
    src = tmp_path / "card.png"
    im = _rgba_with_transparent_corners()
    im.save(src)
    out = P.corner_fill_image(src, (0.0, 0.0, 0.0))
    res = Image.open(out)
    cx, cy = res.width // 2, res.height // 2
    assert res.getpixel((cx, cy)) == (200, 30, 30)  # centre art untouched


def test_fill_colour_is_honoured(tmp_path):
    src = tmp_path / "card.png"
    _rgba_with_transparent_corners().save(src)
    white = P.corner_fill_image(src, (1.0, 1.0, 1.0))
    assert Image.open(white).getpixel((0, 0)) == (255, 255, 255)


def test_different_colours_get_distinct_cache_entries(tmp_path):
    src = tmp_path / "card.png"
    _rgba_with_transparent_corners().save(src)
    black = P.corner_fill_image(src, (0.0, 0.0, 0.0))
    white = P.corner_fill_image(src, (1.0, 1.0, 1.0))
    assert black != white  # colour is part of the cache key


def test_rgb_image_uses_geometric_fallback(tmp_path):
    # No alpha → geometric rounded-corner fill at the standard radius.
    src = tmp_path / "flat.png"
    Image.new("RGB", (745, 1040), (123, 200, 50)).save(src)  # solid green

    out = P.corner_fill_image(src, (0.0, 0.0, 0.0))
    res = Image.open(out)
    assert res.getpixel((0, 0)) == (0, 0, 0)          # corner blacked
    assert res.getpixel((300, 500)) == (123, 200, 50)  # deep interior kept
    # A non-corner edge midpoint must NOT be filled — only the corners.
    assert res.getpixel((0, 520)) == (123, 200, 50)


def test_no_white_sliver_on_flattened_die_cut(tmp_path):
    """Regression: an upscaler flattens the transparent die-cut to solid
    white, so corner-fill hits the geometric path. The fill radius must be
    generous enough that no near-white 'silver line' survives at the curve
    (a 2.5 mm radius left ~245 white px on a real card; 3.2 mm leaves none)."""
    w, h = 1490, 2080  # a real upscaled Scryfall size
    card = Image.new("RGB", (w, h), (20, 20, 20))          # dark card body
    mask = Image.new("L", (w, h), 0)
    # Card die-cut ~2.2 mm; place a white background outside that radius.
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, w - 1, h - 1), radius=52, fill=255)          # ~2.2 mm at this size
    white_bg = Image.new("RGB", (w, h), (255, 255, 255))
    flat = Image.composite(card, white_bg, mask)            # white corners, no alpha
    src = tmp_path / "upscaled.png"
    flat.save(src)

    out = P.corner_fill_image(src, (0.0, 0.0, 0.0))
    res = Image.open(out).convert("RGB")
    near_white = sum(1 for y in range(90) for x in range(90)
                     if min(res.getpixel((x, y))) > 235)
    assert near_white == 0, f"silver line survived: {near_white} near-white px"


def test_no_light_rim_survives_on_alpha_card(tmp_path):
    """Regression: card scans carry a thin light rim right at the die-cut
    edge (opaque, so an alpha composite keeps it) — that rim is the visible
    'silver line'. Corner-fill must blacken a hair into the card edge so the
    rim is gone, not just fill the transparent region outside it."""
    w, h = 745, 1040
    im = Image.new("RGBA", (w, h), (0, 0, 0, 0))     # transparent
    card_mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(card_mask).rounded_rectangle(
        (0, 0, w - 1, h - 1), radius=26, fill=255)   # ~2.2 mm die-cut
    card = Image.new("RGBA", (w, h), (15, 15, 15, 255))
    im.paste(card, (0, 0), card_mask)
    # A bright rim tracing the die-cut edge — the silver line.
    ImageDraw.Draw(im).rounded_rectangle(
        (0, 0, w - 1, h - 1), radius=26, outline=(230, 230, 230, 255), width=3)
    src = tmp_path / "rimmed.png"
    im.save(src)

    out = P.corner_fill_image(src, (0.0, 0.0, 0.0))
    res = Image.open(out).convert("RGB")
    # Scan the corner square only (< die-cut radius): the rounded arc rim
    # lives here. Straight-edge rim (x or y ≥ 26) is out of scope — that's a
    # card-edge concern for bleed, not corner rounding.
    light = sum(1 for y in range(24) for x in range(24)
                if min(res.getpixel((x, y))) > 200)
    assert light == 0, f"silver rim survived in corner: {light} light px"


def test_idempotent_cache_hit_returns_same_path(tmp_path):
    src = tmp_path / "card.png"
    _rgba_with_transparent_corners().save(src)
    first = P.corner_fill_image(src, (0.0, 0.0, 0.0))
    mtime = first.stat().st_mtime_ns
    second = P.corner_fill_image(src, (0.0, 0.0, 0.0))
    assert first == second
    assert second.stat().st_mtime_ns == mtime  # not rewritten
