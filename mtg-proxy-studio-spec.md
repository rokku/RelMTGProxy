# MTG Proxy Studio — Technical Specification

**Purpose:** A local tool that turns a deck list into print-ready proxy PDFs: fetch cards from Scryfall, choose art per card via a visual picker, upscale images for print, and export a PDF with cut guidelines — with optional card backs, including correct back faces for double-faced cards.

**Audience:** This spec is written for Claude Code to implement. It should be built incrementally in the phases described in §9.

---

## 1. Goals & Non-Goals

### Goals
- Parse deck lists in common formats (Moxfield/Archidekt/MTGO plain text) or accept single-card lookups.
- Fetch all available printings per card from Scryfall; let the user pick art visually (click-to-choose grid).
- Upscale chosen images to ~600 DPI effective resolution for inkjet printing.
- Export A4 PDFs, 3×3 grid, cards at exact physical size (63 × 88 mm), with cut lines extended to page edges (paper-trimmer friendly).
- Optional card-back pages: standard MTG back for normal cards, correct back face for transform/MDFC cards, mirrored for duplex alignment. Backs can be interleaved (duplex-ready) or exported as a separate PDF.
- Cache everything (API responses, originals, upscaled images) so re-runs are fast and Scryfall isn't hammered.

### Non-Goals
- No cloud hosting, accounts, or database server. Everything local.
- No deck-building features (no legality checks, no pricing).
- No support for non-standard card sizes (oversized commanders, planes) in v1.
- Not a general image editor — no cropping/colour tools beyond upscaling.

---

## 2. Architecture Overview

Local Python application with a small web UI for the art-picking step.

```
mtg-proxy-studio/
├── proxy_studio/
│   ├── __init__.py
│   ├── decklist.py        # Deck list parsing
│   ├── scryfall.py        # API client (rate-limited, cached)
│   ├── project.py         # Project state (deck + art choices) as JSON
│   ├── upscale.py         # Real-ESRGAN wrapper + DPI checks
│   ├── layout.py          # Page geometry maths (pure functions, unit-tested)
│   ├── pdf_export.py      # ReportLab PDF rendering
│   └── server.py          # FastAPI app serving the picker UI + API
├── static/                # Picker UI (vanilla HTML/JS/CSS, single page)
├── cache/
│   ├── api/               # Scryfall JSON responses
│   ├── images/original/   # Downloaded PNGs, keyed by scryfall_id + face
│   └── images/upscaled/   # Upscaled outputs, keyed by scryfall_id + face + scale
├── projects/              # Saved project JSON files
├── output/                # Exported PDFs
├── cli.py                 # Entry point
└── requirements.txt
```

**Why a local web UI rather than pure CLI:** the art-picking step is inherently visual — comparing 15 printings of Sol Ring in a terminal is miserable. A FastAPI server + single static page gives click-to-choose with zero framework overhead. The rest of the pipeline (fetch, upscale, export) is driven from the CLI or from buttons in the same UI.

**Workflow:**
1. `python cli.py new mazirek.txt` → parses deck list, resolves cards via Scryfall, creates `projects/mazirek.json` with default printings selected.
2. `python cli.py pick mazirek` → launches server, opens browser at `localhost:8787`, user clicks through art choices. Choices save to the project JSON live.
3. `python cli.py export mazirek --backs duplex` → downloads any missing images, upscales, renders PDF(s) to `output/`.

Steps 2 and 3 can also both be triggered from the UI.

---

## 3. Deck List Parsing (`decklist.py`)

Accept plain-text files. Support these line formats (all real-world exports):

```
1 Mazirek, Kraul Death Priest
1x Sol Ring
4 Llanowar Elves (M19) 314
1 Fabled Passage [ELD] 244
2 Swamp <foil>          # decoration tags ignored
// Comment lines and blank lines skipped
SIDEBOARD:               # section headers skipped, contents still parsed
```

Rules:
- Regex-based line parser producing `DeckEntry { quantity, name, set_code?, collector_number? }`.
- If set code + collector number present, that exact printing becomes the default selection.
- Quantity respected in the final PDF (4× Llanowar Elves = 4 slots).
- Unparseable lines are collected and reported at the end — never silently dropped. Exit with a clear list: `Could not parse line 14: "..."`.
- Single-card mode: `python cli.py add mazirek "Nihil Spellbomb"` appends to an existing project.

---

## 4. Scryfall Client (`scryfall.py`)

### Endpoints
- **Resolve name → card:** `GET /cards/named?fuzzy={name}` (fuzzy handles minor typos; on 404, fall back to `GET /cards/search?q=!"{name}"` and report suggestions if any).
- **All printings:** follow the card's `prints_search_uri` (equivalent to `/cards/search?q=oracleid:{id}&unique=prints&order=released`). Paginate via `has_more`/`next_page`.
- **Images:** use `image_uris.png` (745×1040 px, ~300 DPI at card size — the best Scryfall offers). For multi-faced layouts, `image_uris` lives inside `card_faces[]`, not at the top level.

### Etiquette (Scryfall requires this — do not skip)
- Custom `User-Agent` header identifying the app, and `Accept: application/json`.
- Rate limit: max ~10 requests/second; implement a 100 ms minimum delay between requests. A deck of 100 cards with prints lookups is a few hundred requests — throttling matters.
- Cache all JSON responses to `cache/api/` keyed by URL hash, with a 30-day TTL. Never re-fetch a cached printing list within TTL.
- Verify image URLs are still live before render; Scryfall image URIs are stable but the exporter should fail loudly, not embed a broken image.

### Multi-face layouts (get this right — it drives the backs feature)
The `layout` field determines handling:

| layout | Front image | Back image | Notes |
|---|---|---|---|
| `normal`, `saga`, `class`, `adventure`, `split`, `flip`, `leveler` | top-level `image_uris` | standard MTG back | Adventure/split/flip are single physical cards |
| `transform`, `modal_dfc` | `card_faces[0].image_uris` | `card_faces[1].image_uris` | True double-faced — back page must use face 1 |
| `meld` | top-level `image_uris` | meld result (via `all_parts`) *or* standard back | v1: standard back, log a warning; meld result resolution is a v2 nicety |
| `token`, `emblem` | top-level | standard back | Supported if explicitly added |

Store per-entry in the project JSON: `layout`, `front_image_key`, `back_image_key` (nullable → standard back).

---

## 5. Project State (`project.py`)

Single JSON file per deck. This is the source of truth between sessions.

```json
{
  "name": "mazirek",
  "created": "2026-07-03T10:00:00Z",
  "page_settings": { "paper": "A4", "grid": [3, 3], "cut_line_mode": "full-bleed-extensions" },
  "entries": [
    {
      "quantity": 1,
      "name": "Mazirek, Kraul Death Priest",
      "oracle_id": "…",
      "selected_print": { "scryfall_id": "…", "set": "c15", "collector_number": "48" },
      "layout": "normal",
      "back": "standard"
    }
  ]
}
```

Atomic writes (write temp file, rename) so a crash mid-save never corrupts the project.

---

## 6. Art Picker UI (`server.py` + `static/`)

FastAPI serving one static page. No build step, no framework — vanilla JS, matching the standalone-HTML approach of the Commander Oracle tool.

### API
- `GET /api/project` → full project JSON
- `GET /api/prints/{oracle_id}` → all printings (proxied from cache; includes `image_uris.normal` for thumbnails and set/collector metadata)
- `POST /api/select` → `{ entry_index, scryfall_id }` saves selection
- `POST /api/export` → triggers export pipeline, streams progress via Server-Sent Events

### UI behaviour
- Left rail: deck list with thumbnail of current selection per card; cards with unresolved/default-only choices flagged.
- Main panel: clicking a card shows a responsive grid of every printing — `normal`-size thumbnails (488 px wide, fast to load), set symbol + set name + year + collector number captioned under each. Click to select; selection highlights immediately and persists via `POST /api/select`.
- Filter toggles: hide digital-only printings (`digital: true`), hide non-English, group by frame era (old/modern/borderless/showcase/extended).
- DFC cards show both faces in the thumbnail (front large, back small overlay) so art choice accounts for both.
- Keyboard: arrow keys to move between cards, Enter to confirm — makes a 100-card pass fast.

---

## 7. Upscaling (`upscale.py`)

**Honest framing baked into the design:** Scryfall PNGs are 745×1040 px — already ~298 DPI at 63×88 mm. The Epson ET-2860 prints at 5760×1440 optimised dpi but its *useful* photographic resolution on card stock is ~600 DPI. So the target is **2× upscale → 1490×2080 px (~600 DPI)**. Anything beyond 2× is wasted ink-dot resolution and much slower; 4× should exist as an option but not the default.

- **Engine:** Real-ESRGAN, `realesrgan-x4plus` model run at `--outscale 2` (the x4 model downsampled to 2× outperforms dedicated 2× on card art in practice). Use the `realesrgan-ncnn-vulkan` prebuilt binary if no NVIDIA GPU is available (works on CPU/Vulkan, no PyTorch install pain); fall back to the Python `realesrgan` package if a CUDA GPU is detected.
- **Verify at implementation time:** check the current Real-ESRGAN release/binary names before pinning — the project moves and binary asset names have changed between releases.
- Idempotent: output keyed by `{scryfall_id}_{face}_x{scale}.png`; skip if present.
- **DPI check gate (carried over from the batch script):** before export, compute effective DPI of every image at 63×88 mm. Warn below 550, hard-fail below 290 (means the source wasn't the PNG, or something's wrong).
- Preserve colour profile; convert to sRGB explicitly if the source has no embedded profile.

---

## 8. PDF Export (`layout.py` + `pdf_export.py`)

### Geometry (the part that must be exact)
- Card size: **63.0 × 88.0 mm**, hard-coded as the physical target. Images are placed at this size regardless of pixel dimensions — DPI is derived, never assumed.
- Paper: A4 (210 × 297 mm), portrait. 3×3 grid = 189 × 264 mm of cards, leaving 21 mm horizontal / 33 mm vertical margin total.
- Gutter option: `0 mm` (cards touching, single cut per boundary — default, matches paper-trimmer workflow) or configurable gutter for scissor cutting.
- **Cut lines:** thin (0.1 mm) lines drawn **only in the margins**, extending from page edge to the card-region boundary, aligned with every card edge — never drawn across card faces. This is the "cut-line extensions" approach: line up the trimmer against the marginal ticks and cut straight through. Both horizontal and vertical guides on all four margins.
- All geometry lives in `layout.py` as pure functions returning positions in mm → unit tests assert card positions to 0.01 mm. ReportLab works in points; convert once, at the rendering boundary (`mm` from `reportlab.lib.units`).

### Rendering
- ReportLab canvas, images embedded via `drawImage` with explicit width/height in mm-converted points. No resampling on embed — the upscaled PNG goes in as-is.
- One PDF per ~9-card page batch is *not* needed this time — single multi-page PDF per deck, unless `--split-every N` is passed (kept for compatibility with the old 13-PDF workflow if the printer chokes on big files).
- Footer per page (in margin): project name, page `n/m`, date — helps when 12 pages come out of the printer.

### Card backs — three modes (`--backs none|duplex|separate`)
1. **`none`** — fronts only (default).
2. **`duplex`** — pages interleaved: front page 1, back page 1, front page 2, back page 2… ready for duplex printing or manual flip-and-refeed.
3. **`separate`** — second PDF `{name}_backs.pdf` with pages in the same order as the fronts file.

**The critical correctness rule — mirroring:** for backs to align with fronts when the sheet is flipped, each back page must be the **horizontal mirror** of its front page. Card at grid position `(row, col)` on the front goes to `(row, N_cols − 1 − col)` on the back page. The card *images themselves are not mirrored* — only their positions. Assume **long-edge flip** for A4 portrait duplex (this is the horizontal mirror); document in the README that the printer's duplex setting must be "flip on long edge", and expose `--flip-edge short` which mirrors rows instead, for completeness.

Back image per slot:
- `back: "standard"` → a bundled high-res standard MTG card back image (ship one in `assets/`; source a clean ~600 DPI scan — flag to the user that they should supply/approve this asset, don't fabricate provenance).
- `back: face` → the card's `card_faces[1]` image, upscaled through the same pipeline.

Cut lines are drawn on back pages too, mirrored to match.

### Registration test page
`python cli.py testpage` → one-page PDF with a 63×88 mm rectangle grid and crosshairs front and back (mirrored). Print it duplex, hold to the light, measure the offset. If the printer drifts, `--back-offset-x/-y` (mm, can be negative) shifts all back-page content to compensate. This single feature saves more misprinted card stock than anything else in the spec.

---

## 9. Implementation Phases

**Phase 1 — Pipeline core (no UI):** deck parsing, Scryfall client with caching + rate limiting, project JSON, download default printings, fronts-only PDF with correct geometry and cut lines. Unit tests for `decklist.py` and `layout.py`. *Deliverable: deck list in → printable fronts PDF out.*

**Phase 2 — Upscaling:** Real-ESRGAN integration, idempotent cache, DPI gate, sRGB handling.

**Phase 3 — Art picker:** FastAPI server, static UI, prints browsing, selection persistence, export trigger with progress.

**Phase 4 — Backs:** standard back, DFC back faces, duplex/separate modes, mirroring maths (unit-tested), registration test page and offset compensation.

**Phase 5 — Polish:** meld resolution, `--split-every`, non-A4 paper (Letter), gutter+crop-mark mode for full-bleed printing.

Each phase should end runnable and tested before the next begins.

---

## 10. Edge Cases & Failure Behaviour

- **Card not found:** report the fuzzy suggestions Scryfall returns; never guess silently.
- **Ambiguous name** (e.g. "Bala Ged Recovery" resolves fine, but partial names may not): fail with candidates listed.
- **Basic lands:** hundreds of printings — the picker must paginate or lazy-load thumbnails; don't fetch 600 images eagerly.
- **Art Series / digital-only printings:** filtered out by default (`digital: true`, `set_type: "memorabilia"` excluded), toggleable.
- **Network failure mid-export:** resume-safe — every download and upscale is idempotent and keyed, so re-running export continues where it stopped.
- **Duplicate entries in deck list:** merge quantities for the same name unless different printings are pinned.
- **Odd final page:** fewer than 9 cards — remaining slots empty, cut lines still drawn for occupied columns/rows only.

## 11. Dependencies

`requests`, `fastapi`, `uvicorn`, `reportlab`, `Pillow`, Real-ESRGAN (ncnn-vulkan binary or `realesrgan` + `torch` if CUDA). Pin versions in `requirements.txt` at implementation time after verifying current releases — do not trust remembered version numbers.

## 12. Acceptance Criteria

1. A 100-card Commander deck list exports to a fronts PDF where a printed card measures 63 × 88 mm ± 0.5 mm.
2. Cut lines appear only in margins and align with card edges on every page.
3. Duplex export: after a long-edge duplex print, every back sits behind its own front (verified with the registration test page).
4. A transform card (e.g. Delver of Secrets) shows its back face — not the standard back — in the correct mirrored slot.
5. Re-running export with a warm cache performs zero Scryfall requests and zero re-upscales.
6. Effective print DPI ≥ 550 for every image, enforced by the gate.
