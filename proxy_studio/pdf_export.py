"""PDF rendering.

Geometry lives in `layout.py` in mm; we convert to points here at the render
boundary, as spec §8 requires.

Three back modes (spec §8):
- `none`     — fronts only (default; behaviour of Phase 1).
- `duplex`   — pages interleaved: F1 B1 F2 B2 … ready for duplex printing.
- `separate` — a second PDF `{name}_backs.pdf` with pages in the same order
  as the fronts file.

Mirroring — *positions only*, not the images themselves. A card at grid
(row, col) on the front sits at (row, cols-1-col) on the back (long-edge
flip). `--back-offset-x/-y` shifts the whole back-page composition to
compensate for printer drift; the registration test page (`cli.py testpage`)
is how you measure that drift.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, Literal, Sequence

from reportlab.lib.units import mm
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4, LETTER

from . import layout as L

log = logging.getLogger(__name__)

BacksMode = Literal["none", "duplex", "separate"]

# Default cut-guide colour: subtle cyan-blue.
DEFAULT_CUT_COLOR: tuple[float, float, float] = (0.30, 0.55, 0.90)


@dataclass(frozen=True)
class RenderCard:
    """One card image + how many times to repeat it.

    `back_image_path` is None for `none` mode; a real path for duplex/separate.
    """
    image_path: Path
    name: str
    quantity: int = 1
    back_image_path: Path | None = None


# --- Public API -------------------------------------------------------------

def render_pdf(cards: Sequence[RenderCard], out_path: str | Path,
               *, project_name: str = "",
               spec: L.PageSpec | None = None,
               backs_mode: BacksMode = "none",
               flip_edge: L.FlipEdge = "long",
               back_offset_x_mm: float = 0.0,
               back_offset_y_mm: float = 0.0,
               cut_color: tuple[float, float, float] = DEFAULT_CUT_COLOR,
               cut_line_width_mm: float = 0.15,
               dpi_warn: float = 550.0,
               dpi_fail: float = 290.0) -> tuple[Path, Path | None]:
    """Render the fronts (and optionally the backs) into `out_path`.

    Returns `(fronts_pdf, backs_pdf_or_none)`. In `duplex` mode `backs_pdf`
    is None because everything is interleaved into the fronts file. In
    `separate` mode `backs_pdf` is `{stem}_backs.pdf` next to `out_path`.
    """
    spec = spec or L.PageSpec()
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    flat = _expand_quantities(cards)
    pages = _chunk(flat, spec.slots_per_page)

    # --- Duplex mode: single file, interleaved F/B ------------------------
    if backs_mode == "duplex":
        _validate_backs_present(flat, mode="duplex")
        c = _new_canvas(out_path, spec, project_name)
        _draw_all(c, pages, spec, backs=True,
                  flip_edge=flip_edge,
                  back_offset=(back_offset_x_mm, back_offset_y_mm),
                  interleave=True,
                  cut_color=cut_color, cut_line_width_mm=cut_line_width_mm,
                  dpi_warn=dpi_warn, dpi_fail=dpi_fail,
                  project_name=project_name)
        c.save()
        return out_path, None

    # --- Fronts pass (always) ---------------------------------------------
    c = _new_canvas(out_path, spec, project_name)
    _draw_all(c, pages, spec, backs=False, flip_edge=flip_edge,
              back_offset=(0.0, 0.0), interleave=False,
              cut_color=cut_color, cut_line_width_mm=cut_line_width_mm,
              dpi_warn=dpi_warn, dpi_fail=dpi_fail,
              project_name=project_name)
    c.save()

    if backs_mode == "none":
        return out_path, None

    # --- Separate: second file, backs only ---------------------------------
    _validate_backs_present(flat, mode="separate")
    base_stem = out_path.stem.removesuffix("_fronts")
    backs_path = out_path.with_name(f"{base_stem}_backs.pdf")
    cb = _new_canvas(backs_path, spec, f"{project_name} (backs)")
    _draw_all(cb, pages, spec, backs=True, flip_edge=flip_edge,
              back_offset=(back_offset_x_mm, back_offset_y_mm),
              interleave=False, only_backs=True,
              cut_color=cut_color, cut_line_width_mm=cut_line_width_mm,
              dpi_warn=dpi_warn, dpi_fail=dpi_fail,
              project_name=f"{project_name} (backs)")
    cb.save()
    return out_path, backs_path


# Back-compat alias — existing callers use `render_fronts_pdf(...)`.
def render_fronts_pdf(cards, out_path, **kwargs):
    fronts, _ = render_pdf(cards, out_path, backs_mode="none", **kwargs)
    return fronts


# --- Canvas + draw ----------------------------------------------------------

def _new_canvas(path: Path, spec: L.PageSpec, title: str) -> canvas.Canvas:
    page_w_mm, page_h_mm = spec.page_size_mm
    c = canvas.Canvas(str(path), pagesize=(page_w_mm * mm, page_h_mm * mm))
    c.setTitle(title or path.stem)
    return c


def _draw_all(c: canvas.Canvas,
              pages: list[list[RenderCard]],
              spec: L.PageSpec, *,
              backs: bool,
              flip_edge: L.FlipEdge,
              back_offset: tuple[float, float],
              interleave: bool,
              only_backs: bool = False,
              cut_color, cut_line_width_mm, dpi_warn, dpi_fail,
              project_name) -> None:
    """Draw fronts and/or backs into an existing canvas."""
    front_slots = L.page_slots(spec)
    back_slots = L.mirror_slots_for_back(front_slots, spec, flip_edge=flip_edge)
    total_pages = len(pages) * (2 if interleave else 1)
    page_no = 0

    for page_idx, chunk in enumerate(pages):
        # Front page (unless we're rendering only backs into a separate file).
        if not only_backs:
            page_no += 1
            _draw_cut_marks(c, spec, occupied=len(chunk),
                            color=cut_color, width_mm=cut_line_width_mm)
            for i, card in enumerate(chunk):
                _draw_card(c, card.image_path, front_slots[i], card.name,
                           dpi_warn=dpi_warn, dpi_fail=dpi_fail)
            _draw_footer(c, spec, project_name, page_no, total_pages,
                          suffix="")
            c.showPage()

        # Back page: always drawn if backs is True (interleave OR only_backs).
        if backs:
            page_no += 1
            # Cut lines shift with the back-page content so they still align
            # with the back-image edges after any offset compensation.
            _draw_cut_marks(c, spec, occupied=len(chunk),
                            color=cut_color, width_mm=cut_line_width_mm,
                            offset_mm=back_offset)
            for i, card in enumerate(chunk):
                if card.back_image_path is None:
                    raise ValueError(
                        f"Card {card.name!r} has no back image but a back "
                        "page is being rendered."
                    )
                slot = _apply_offset(back_slots[i], back_offset)
                _draw_card(c, card.back_image_path, slot, card.name,
                           dpi_warn=dpi_warn, dpi_fail=dpi_fail)
            _draw_footer(c, spec, project_name, page_no, total_pages,
                          suffix=" (back)")
            c.showPage()


def _apply_offset(slot: L.SlotRect, offset: tuple[float, float]) -> L.SlotRect:
    dx, dy = offset
    return replace(slot, x_mm=slot.x_mm + dx, y_mm=slot.y_mm + dy)


def _draw_card(c: canvas.Canvas, image_path: Path, slot: L.SlotRect,
                name: str, *, dpi_warn: float, dpi_fail: float) -> None:
    _check_dpi(image_path, name, dpi_warn=dpi_warn, dpi_fail=dpi_fail)
    c.drawImage(
        str(image_path),
        x=slot.x_mm * mm, y=slot.y_mm * mm,
        width=L.CARD_W_MM * mm, height=L.CARD_H_MM * mm,
        preserveAspectRatio=False, mask="auto",
    )


def _draw_cut_marks(c: canvas.Canvas, spec: L.PageSpec, *, occupied: int,
                    color: tuple[float, float, float],
                    width_mm: float,
                    offset_mm: tuple[float, float] = (0.0, 0.0)) -> None:
    dx, dy = offset_mm
    c.setLineWidth(width_mm * mm)
    c.setStrokeColorRGB(*color)
    for tick in L.cut_marks(spec, occupied_slot_count=occupied):
        c.line((tick.x1_mm + dx) * mm, (tick.y1_mm + dy) * mm,
               (tick.x2_mm + dx) * mm, (tick.y2_mm + dy) * mm)


def _draw_footer(c: canvas.Canvas, spec: L.PageSpec,
                 project_name: str, page_idx: int, total: int,
                 *, suffix: str = "") -> None:
    left, _r, _t, bottom = spec.margins_mm
    y_mm = bottom / 2.0
    c.setFont("Helvetica", 7)
    c.setFillColorRGB(0.2, 0.2, 0.2)
    text = f"{project_name}  ·  page {page_idx}/{total}{suffix}  ·  {date.today().isoformat()}"
    c.drawString(left * mm, y_mm * mm, text)


# --- Helpers ---------------------------------------------------------------

def _expand_quantities(cards: Iterable[RenderCard]) -> list[RenderCard]:
    out: list[RenderCard] = []
    for card in cards:
        out.extend(
            [RenderCard(card.image_path, card.name, 1, card.back_image_path)]
            * card.quantity
        )
    return out


def _chunk(items: list[RenderCard], size: int) -> list[list[RenderCard]]:
    return [items[i:i + size] for i in range(0, len(items), size)] or [[]]


def _validate_backs_present(cards: Iterable[RenderCard], *, mode: str) -> None:
    missing = [c.name for c in cards if c.back_image_path is None]
    if missing:
        raise ValueError(
            f"{mode} back rendering requested but these cards have no back "
            f"image resolved: {', '.join(sorted(set(missing))[:5])}"
            + (" …" if len(set(missing)) > 5 else "")
        )


def _check_dpi(image_path: Path, name: str, *,
               dpi_warn: float, dpi_fail: float) -> None:
    from PIL import Image
    with Image.open(image_path) as im:
        w, h = im.size
    dpi_x, dpi_y = L.effective_dpi(w, h)
    lowest = min(dpi_x, dpi_y)
    if lowest < dpi_fail:
        raise ValueError(
            f"Effective DPI {lowest:.0f} for {name!r} is below hard-fail "
            f"threshold {dpi_fail:.0f}."
        )
    if lowest < dpi_warn:
        log.warning("DPI %.0f for %s below warn threshold %.0f (image %dx%d).",
                    lowest, name, dpi_warn, w, h)


REPORTLAB_PAGESIZES = {"A4": A4, "Letter": LETTER}


def render_registration_test(out_path: str | Path, *,
                              spec: L.PageSpec | None = None,
                              flip_edge: L.FlipEdge = "long",
                              back_offset_x_mm: float = 0.0,
                              back_offset_y_mm: float = 0.0,
                              cut_color: tuple[float, float, float] = DEFAULT_CUT_COLOR,
                              ) -> Path:
    """Render a two-page duplex registration test.

    Front and back both draw a full 3×3 grid of card-shaped rectangles with
    a crosshair through each slot centre. Duplex-print on plain paper, hold
    to the light, and any drift between front and back marks tells you what
    to pass as `--back-offset-x/-y` at export time.
    """
    spec = spec or L.PageSpec()
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    c = _new_canvas(out_path, spec, "Registration test")

    front_slots = L.page_slots(spec)
    back_slots = L.mirror_slots_for_back(front_slots, spec, flip_edge=flip_edge)
    offset = (back_offset_x_mm, back_offset_y_mm)

    for page in range(2):
        is_back = page == 1
        slots = back_slots if is_back else front_slots

        _draw_cut_marks(c, spec, occupied=spec.slots_per_page,
                        color=cut_color, width_mm=0.15,
                        offset_mm=offset if is_back else (0.0, 0.0))

        c.setStrokeColorRGB(0.0, 0.0, 0.0)
        c.setLineWidth(0.2 * mm)
        for slot in slots:
            s = _apply_offset(slot, offset) if is_back else slot
            # Card outline.
            c.rect(s.x_mm * mm, s.y_mm * mm,
                    L.CARD_W_MM * mm, L.CARD_H_MM * mm, stroke=1, fill=0)
            # Crosshair at slot centre.
            cx = (s.x_mm + L.CARD_W_MM / 2) * mm
            cy = (s.y_mm + L.CARD_H_MM / 2) * mm
            arm = 8 * mm
            c.line(cx - arm, cy, cx + arm, cy)
            c.line(cx, cy - arm, cx, cy + arm)
            # Label so front/back are unambiguous when the sheet flips.
            c.setFont("Helvetica", 6)
            c.setFillColorRGB(0.4, 0.4, 0.4)
            label = f"{'B' if is_back else 'F'} r{slot.row}c{slot.col}"
            c.drawString(s.x_mm * mm + 2 * mm,
                          s.y_mm * mm + 2 * mm, label)
            c.setStrokeColorRGB(0.0, 0.0, 0.0)

        _draw_footer(c, spec, "Registration test",
                     page + 1, 2, suffix=" (back)" if is_back else "")
        c.showPage()

    c.save()
    return out_path


def default_output_path(project_name: str, backs_mode: BacksMode,
                         output_dir: str | Path = "output",
                         *, timestamp: bool = True,
                         now: datetime | None = None) -> Path:
    """Where an export should land by default.

    With `timestamp=True`, filenames carry a `YYYY-MM-DD-HHMM` suffix so
    successive exports don't overwrite each other and you keep a history
    of every render. The suffix uses '-' rather than ':' for filesystem
    portability.

    Naming rules:
      - duplex mode   → `{name}_{ts}.pdf`         (one interleaved file)
      - other modes   → `{name}_{ts}_fronts.pdf`  (backs live in the sibling
                                                   `_backs.pdf` written by
                                                   `render_pdf` itself)
    """
    ts = ""
    if timestamp:
        now = now or datetime.now()
        ts = f"_{now.strftime('%Y-%m-%d-%H%M')}"
    out_dir = Path(output_dir)
    if backs_mode == "duplex":
        return out_dir / f"{project_name}{ts}.pdf"
    return out_dir / f"{project_name}{ts}_fronts.pdf"


def parse_hex_color(hex_str: str) -> tuple[float, float, float]:
    s = hex_str.lstrip("#")
    if len(s) == 3:
        s = "".join(ch * 2 for ch in s)
    if len(s) != 6:
        raise ValueError(f"expected #rrggbb, got {hex_str!r}")
    r, g, b = int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)
    return (r / 255.0, g / 255.0, b / 255.0)
