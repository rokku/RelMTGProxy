# MTG Proxy Studio

Local tool that turns a decklist into print-ready proxy PDFs. See
[`mtg-proxy-studio-spec.md`](./mtg-proxy-studio-spec.md) for the full spec.

## Status

Building in phases per §9 of the spec.

- **Phase 1 (in progress):** decklist parsing → Scryfall fetch → fronts-only A4
  PDF with correct 63×88 mm geometry and marginal cut ticks.
- Phase 2: Real-ESRGAN upscaling + DPI gate.
- Phase 3: FastAPI art-picker UI.
- Phase 4: card backs (standard, DFC face 1) with mirrored duplex layout +
  registration test page.
- Phase 5: polish (meld, Letter paper, gutters, split-every).

## Setup

```
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Usage (Phase 1)

```
python cli.py new decks/mazirek.txt       # create project from decklist
python cli.py add mazirek "Sol Ring"      # append a single card
python cli.py export mazirek              # render fronts PDF into output/
```

## Duplex printing note

When Phase 4 lands, back pages are mirrored assuming **long-edge flip** (A4
portrait). Set your printer's duplex option accordingly, or pass
`--flip-edge short` at export time.
