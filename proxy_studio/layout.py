"""Page geometry maths.

Pure functions returning positions in millimetres. No I/O, no ReportLab imports
— the PDF renderer converts to points at its own boundary. Unit-tested to
0.01 mm because a 1 mm drift on a 63 mm-wide card is a visibly-off proxy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

# --- Fixed physical constants ------------------------------------------------
CARD_W_MM: float = 63.0
CARD_H_MM: float = 88.0

PAPERS_MM: dict[str, tuple[float, float]] = {
    "A4": (210.0, 297.0),
    "Letter": (215.9, 279.4),
}

FlipEdge = Literal["long", "short"]
CutLineMode = Literal["ticks", "full"]


@dataclass(frozen=True)
class PageSpec:
    paper: str = "A4"
    cols: int = 3
    rows: int = 3
    # Default: 3 mm gutter — matches the user's paper-trimmer workflow
    # (see project memory `pdf_export_defaults`). Set to 0 for touching cards.
    gutter_mm: float = 3.0
    # "full" draws page-edge-to-page-edge cut guides through the gutters.
    # "ticks" restricts guides to the margins only (spec §8 original mode).
    cut_line_mode: CutLineMode = "full"

    @property
    def page_size_mm(self) -> tuple[float, float]:
        try:
            return PAPERS_MM[self.paper]
        except KeyError as e:
            raise ValueError(f"Unknown paper size: {self.paper!r}") from e

    @property
    def grid_size_mm(self) -> tuple[float, float]:
        w = self.cols * CARD_W_MM + (self.cols - 1) * self.gutter_mm
        h = self.rows * CARD_H_MM + (self.rows - 1) * self.gutter_mm
        return (w, h)

    @property
    def margins_mm(self) -> tuple[float, float, float, float]:
        """(left, right, top, bottom) — grid centred on the page."""
        pw, ph = self.page_size_mm
        gw, gh = self.grid_size_mm
        left = (pw - gw) / 2.0
        top = (ph - gh) / 2.0
        return (left, left, top, top)

    @property
    def slots_per_page(self) -> int:
        return self.cols * self.rows


@dataclass(frozen=True)
class SlotRect:
    """A card slot on a page. Coordinates in mm, origin = bottom-left of page.

    ReportLab uses bottom-left origin, so we match that convention throughout
    the pipeline to avoid off-by-flip bugs at the render boundary.
    """
    col: int
    row: int
    x_mm: float
    y_mm: float
    w_mm: float = CARD_W_MM
    h_mm: float = CARD_H_MM

    @property
    def x_right_mm(self) -> float:
        return self.x_mm + self.w_mm

    @property
    def y_top_mm(self) -> float:
        return self.y_mm + self.h_mm


def page_slots(spec: PageSpec) -> list[SlotRect]:
    """Return the slot rectangles in row-major, top-to-bottom reading order.

    Reading order matters for interleaving cards into pages predictably: index
    0 is top-left, index (cols*rows - 1) is bottom-right.
    """
    pw, ph = spec.page_size_mm
    left, _right, top, _bottom = spec.margins_mm
    slots: list[SlotRect] = []
    for r in range(spec.rows):
        # r=0 is the top row visually; convert to bottom-left origin.
        y_top_of_row = ph - top - r * (CARD_H_MM + spec.gutter_mm)
        y = y_top_of_row - CARD_H_MM
        for c in range(spec.cols):
            x = left + c * (CARD_W_MM + spec.gutter_mm)
            slots.append(SlotRect(col=c, row=r, x_mm=x, y_mm=y))
    return slots


def mirror_slots_for_back(slots: list[SlotRect], spec: PageSpec,
                          flip_edge: FlipEdge = "long") -> list[SlotRect]:
    """Return slots repositioned so a duplex back page aligns with its front.

    - `flip_edge="long"` (A4 portrait duplex default): horizontal mirror →
      column c goes to column (cols-1-c). The card image itself is not
      flipped; only its X position changes.
    - `flip_edge="short"`: vertical mirror → row r goes to (rows-1-r).

    Input order is preserved so that `mirrored[i]` is the destination slot for
    the card whose front lives at `slots[i]`.
    """
    if flip_edge == "long":
        return [_mirror_x(s, spec) for s in slots]
    if flip_edge == "short":
        return [_mirror_y(s, spec) for s in slots]
    raise ValueError(f"flip_edge must be 'long' or 'short', got {flip_edge!r}")


def _mirror_x(slot: SlotRect, spec: PageSpec) -> SlotRect:
    new_col = spec.cols - 1 - slot.col
    # Find the X of the destination column directly from geometry (avoids
    # accumulating float error via subtracting from page width).
    left, _r, _t, _b = spec.margins_mm
    x = left + new_col * (CARD_W_MM + spec.gutter_mm)
    return SlotRect(col=new_col, row=slot.row, x_mm=x, y_mm=slot.y_mm)


def _mirror_y(slot: SlotRect, spec: PageSpec) -> SlotRect:
    new_row = spec.rows - 1 - slot.row
    _pw, ph = spec.page_size_mm
    _l, _r, top, _b = spec.margins_mm
    y_top_of_row = ph - top - new_row * (CARD_H_MM + spec.gutter_mm)
    y = y_top_of_row - CARD_H_MM
    return SlotRect(col=slot.col, row=new_row, x_mm=slot.x_mm, y_mm=y)


@dataclass(frozen=True)
class CutTick:
    """A single tick line segment in the margin, mm coords, page origin BL."""
    x1_mm: float
    y1_mm: float
    x2_mm: float
    y2_mm: float


def cut_ticks(spec: PageSpec, occupied_slot_count: int | None = None) -> list[CutTick]:
    """Ticks in the margins only — never crossing card faces.

    Vertical ticks aligned to each column edge (top + bottom margins).
    Horizontal ticks aligned to each row edge (left + right margins).

    If `occupied_slot_count` is given, ticks only span rows/columns that
    contain at least one occupied slot (spec §10: odd final page).
    """
    pw, ph = spec.page_size_mm
    left, right_m, top, bottom_m = spec.margins_mm
    gw, gh = spec.grid_size_mm
    right_edge_x = left + gw
    bottom_edge_y = ph - top - gh  # y of the grid's bottom edge

    # Determine which columns/rows are occupied.
    if occupied_slot_count is None:
        occ_rows = set(range(spec.rows))
        occ_cols = set(range(spec.cols))
    else:
        occ_rows = set()
        occ_cols = set()
        for i in range(min(occupied_slot_count, spec.slots_per_page)):
            occ_rows.add(i // spec.cols)
            occ_cols.add(i % spec.cols)

    ticks: list[CutTick] = []

    # X positions for each vertical card-edge.
    x_edges: list[tuple[float, int, int]] = []  # (x, left_col, right_col)
    for c in range(spec.cols + 1):
        x = left + c * (CARD_W_MM + spec.gutter_mm)
        # Column indices on either side of this edge (or -1 if outside grid).
        lc = c - 1 if c > 0 else -1
        rc = c if c < spec.cols else -1
        x_edges.append((x, lc, rc))

    # Y positions for each horizontal card-edge (in bottom-left coords).
    y_edges: list[tuple[float, int, int]] = []
    for r in range(spec.rows + 1):
        # r counts from top visually.
        y = ph - top - r * (CARD_H_MM + spec.gutter_mm)
        tr = r - 1 if r > 0 else -1   # row above the edge (visual)
        br = r if r < spec.rows else -1
        y_edges.append((y, tr, br))

    # Vertical ticks (top + bottom margins): only for occupied columns.
    for x, lc, rc in x_edges:
        active = (lc in occ_cols) or (rc in occ_cols)
        if not active:
            continue
        # Top margin tick: from top of grid up to top of page.
        ticks.append(CutTick(x, ph - top, x, ph))
        # Bottom margin tick: from bottom of page up to bottom of grid.
        ticks.append(CutTick(x, 0.0, x, bottom_edge_y))

    # Horizontal ticks (left + right margins): only for occupied rows.
    for y, tr, br in y_edges:
        active = (tr in occ_rows) or (br in occ_rows)
        if not active:
            continue
        # Left margin tick.
        ticks.append(CutTick(0.0, y, left, y))
        # Right margin tick.
        ticks.append(CutTick(right_edge_x, y, pw, y))

    return ticks


def cut_lines_full(spec: PageSpec,
                   occupied_slot_count: int | None = None) -> list[CutTick]:
    """Full-length cut guides — one line at every card edge.

    Every card has four edges. This function emits a page-edge-to-page-edge
    guide aligned with each of those four edges (deduplicated when adjacent
    cards share an edge at `gutter_mm == 0`). With a non-zero gutter, each
    card gets two visible vertical guides (its left and right edges) and two
    horizontal guides (its top and bottom edges), so a paper-trimmer can be
    aligned against any of the four cuts.

    Guides pass under the card artwork; the visible portions live in the
    gutters and margins, which is where you actually align the trimmer.

    When `occupied_slot_count` is given, only rows/columns with at least one
    occupied slot get guides, so an odd final page doesn't grow phantom
    lines below its last card.
    """
    pw, ph = spec.page_size_mm
    left, _r, top, _b = spec.margins_mm

    if occupied_slot_count is None:
        occ_rows = set(range(spec.rows))
        occ_cols = set(range(spec.cols))
    else:
        occ_rows, occ_cols = set(), set()
        for i in range(min(occupied_slot_count, spec.slots_per_page)):
            occ_rows.add(i // spec.cols)
            occ_cols.add(i % spec.cols)

    # Collect unique x positions: LEFT and RIGHT edge of every occupied col.
    # A `set` collapses shared edges at gutter=0 to a single line.
    xs: set[float] = set()
    for c in sorted(occ_cols):
        col_left = left + c * (CARD_W_MM + spec.gutter_mm)
        xs.add(round(col_left, 4))
        xs.add(round(col_left + CARD_W_MM, 4))

    # Same treatment for rows: TOP and BOTTOM edge of every occupied row.
    ys: set[float] = set()
    for r in sorted(occ_rows):
        row_top = ph - top - r * (CARD_H_MM + spec.gutter_mm)
        ys.add(round(row_top, 4))
        ys.add(round(row_top - CARD_H_MM, 4))

    lines: list[CutTick] = []
    for x in sorted(xs):
        lines.append(CutTick(x, 0.0, x, ph))
    for y in sorted(ys):
        lines.append(CutTick(0.0, y, pw, y))
    return lines


def cut_marks(spec: PageSpec,
              occupied_slot_count: int | None = None) -> list[CutTick]:
    """Dispatch to `cut_ticks` or `cut_lines_full` based on `spec.cut_line_mode`."""
    if spec.cut_line_mode == "ticks":
        return cut_ticks(spec, occupied_slot_count)
    if spec.cut_line_mode == "full":
        return cut_lines_full(spec, occupied_slot_count)
    raise ValueError(f"Unknown cut_line_mode: {spec.cut_line_mode!r}")


def effective_dpi(pixel_w: int, pixel_h: int,
                  card_w_mm: float = CARD_W_MM,
                  card_h_mm: float = CARD_H_MM) -> tuple[float, float]:
    """(dpi_x, dpi_y) at which the image would print in a card-sized slot."""
    inch = 25.4
    return (pixel_w * inch / card_w_mm, pixel_h * inch / card_h_mm)


@dataclass
class PageBatch:
    """A batched set of card entries mapped to a paginated layout."""
    spec: PageSpec
    # Each inner list is a page: (slot_index_on_page, card_key) pairs.
    pages: list[list[tuple[int, str]]] = field(default_factory=list)


def paginate(card_keys: list[str], spec: PageSpec) -> PageBatch:
    """Split a flat list of card identifiers into pages of `slots_per_page`.

    `card_keys` is opaque to layout — the renderer maps them back to images.
    """
    per_page = spec.slots_per_page
    pages: list[list[tuple[int, str]]] = []
    for i in range(0, len(card_keys), per_page):
        chunk = card_keys[i:i + per_page]
        pages.append(list(enumerate(chunk)))
    return PageBatch(spec=spec, pages=pages)
