# RelMTG Proxy

Turn a Magic: the Gathering decklist into a print-ready PDF of proxies —
right on your Mac, no accounts or cloud services. Paste a list, pick the
art you like, hit export, print, cut, play.

![RelMTG Proxy](assets/relmtgproxy_logo.png)

## What you need

- **A Mac** (works on Apple Silicon and Intel).
- **Python 3.10 or newer.** Check by opening Terminal and running
  `python3 --version`. If it says something older than 3.10, install a
  newer version from [python.org](https://www.python.org/downloads/) or
  via Homebrew (`brew install python`).
- **An internet connection** for the first run (to fetch card images
  from Scryfall). After that it works offline for anything you've
  already looked at.
- **A printer** that can do full-colour A4 output. Duplex printing is
  optional but nice for card backs.

## One-time setup

Open Terminal, `cd` into the project folder, then paste these commands
one at a time:

```bash
# 1. Create a Python virtual environment (keeps things tidy).
python3 -m venv .venv

# 2. Activate it (you'll need to do this every new Terminal window).
source .venv/bin/activate

# 3. Install the required packages (~50 MB).
pip install -r requirements.txt
```

That's it — you can now run the tool.

## Running the app

From the project folder, with the virtual environment activated:

```bash
python cli.py pick
```

That command starts a small local web server and opens
**[http://127.0.0.1:8787](http://127.0.0.1:8787)** in your browser. If it
doesn't open automatically, click that link.

To stop the server, come back to the Terminal window and press **Ctrl-C**.

To start it again another day:

```bash
cd /path/to/RelMTGProxy
source .venv/bin/activate
python cli.py pick
```

## Using the app

Once the browser page opens:

1. **Click `+ New`** in the left-hand Projects panel.
2. **Give the deck a name** (e.g. "Mazirek Sacrifice") and paste in one
   of these:
   - A decklist (from Moxfield, Archidekt, MTGO — plain text with one
     card per line, quantity first: `1 Sol Ring`).
   - A Moxfield deck URL like `https://www.moxfield.com/decks/ABC123`.
   - Or drop card images onto the "Drop card art here" box.
3. **Hit Create project.** RelMTG Proxy resolves each card on Scryfall
   and drops you into the deck view — a grid of every card's chosen art.
4. **Click any card** to open the art picker. Every alternate printing
   Scryfall knows about is shown; click one to swap the art. Filter
   chips (Hide digital / English only / Frame era) narrow the choices.
5. **Drag cards around** in the grid to reorder them — the order maps
   directly to the printed pages.
6. **Hit `Export PDF`** in the top-right. A green download button
   appears when it's done — click it to save the PDF, then send it to
   your printer.

Prints are laid out 3×3 per A4 sheet at the correct MTG card size
(63 × 88 mm) with cut guides between each card.

## Optional: higher-quality prints (600 DPI)

Scryfall's images are ~300 DPI at card size. That looks OK; **600 DPI**
looks like a real card. Turning this on takes a one-time download.

Two options depending on your Mac:

### Option A — universal, works everywhere (~50 MB)

```bash
python cli.py setup-upscaler
```

This downloads the Real-ESRGAN upscaler. From then on, every export
runs each card through it. On an Apple Silicon Mac this is ~25 seconds
per card the first time; the second export is instant because results
are cached.

### Option B — faster on Apple Silicon (~700 MB)

If you have an M-series Mac and want a bit more speed:

```bash
pip install -r requirements-mps.txt          # installs PyTorch (~600 MB)
python cli.py setup-upscaler --backend mps   # downloads the model (~64 MB)
```

Now the app picks the faster native backend automatically. You can
turn upscaling off per-export via the **"600 DPI upscale"** checkbox
next to the Export button.

## Optional: card backs

The default export is fronts only. In the top toolbar, the **Backs**
dropdown lets you include the standard brown card back:

- **None** — fronts only (default).
- **Duplex (interleaved)** — one PDF with front, back, front, back… for
  duplex printing. In your printer settings pick **"Flip on long
  edge"** so the backs line up.
- **Separate file** — writes `<deck>_backs.pdf` alongside the fronts
  PDF so you can print them by hand-flipping the paper.

The first time you export with backs it uses a placeholder card back
image with a "PLACEHOLDER" watermark. To replace it with a real one,
drop a clean high-resolution card back scan at
`assets/mtg_back.png` and it'll be used from then on.

If your printer's duplex alignment is slightly off, run:

```bash
python cli.py testpage
```

Duplex-print the resulting `output/registration_test.pdf` on plain
paper, hold it to a lamp, and measure how far the back marks are from
the front marks. Then feed those numbers back at export time:

```bash
python cli.py export "Mazirek Sacrifice" --backs duplex \
    --back-offset-x 1.5 --back-offset-y -0.8
```

## Where things end up

- `output/` — every PDF you export, timestamped so you keep a history.
- `projects/` — one JSON file per deck, safe to back up.
- `cache/` — downloaded card images, upscaled versions, uploaded art.
  Nothing here is precious; deleting the folder just makes the next
  export slower while things re-cache.

## Command-line, for the curious

Everything you can do in the browser you can also do from Terminal:

```bash
python cli.py new decks/mazirek.txt          # create from a decklist file
python cli.py new https://moxfield.com/decks/ABC123  # or a Moxfield URL
python cli.py add "Mazirek Sacrifice" "Sol Ring"     # add one card
python cli.py export "Mazirek Sacrifice"             # render the PDF
python cli.py pick                                    # open the web UI
```

For the full list, `python cli.py --help`.

## Troubleshooting

**"Command not found: python3"** — install Python from
python.org or via Homebrew (`brew install python`), close and reopen
Terminal, try again.

**"No module named …"** — you likely forgot to activate the virtual
environment. Run `source .venv/bin/activate` first.

**Browser doesn't open automatically** — copy
`http://127.0.0.1:8787` into any browser tab.

**Export button spins forever** — check the Terminal window. If a card
lookup failed on Scryfall you'll see the error there. Delete the card,
re-add with the exact name, try again.

**Cards look pixelated in the printed PDF** — you probably have
upscaling turned off. Check the "600 DPI upscale" box next to Export
and make sure the upscaler is installed (`python cli.py setup-upscaler`).

## What's under the hood (specification)

See [`mtg-proxy-studio-spec.md`](./mtg-proxy-studio-spec.md) for the
original design doc that describes every subsystem in detail.
