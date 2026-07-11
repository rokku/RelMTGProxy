# RelMTG Proxy

Turn a Magic: the Gathering decklist into a print-ready PDF of proxies —
right on your own machine, no accounts or cloud services. Paste a list,
pick the art you like, hit export, print, cut, play.

![RelMTG Proxy](assets/relmtgproxy_logo.png)

Runs on **macOS, Windows, and Linux**.

> **Cross-platform status (as of 2026-07):** the code is written to be
> portable and the test suite (235 tests) runs green on macOS, but the
> maintainer has only used it on Apple Silicon in anger. Windows and
> Linux support was added in a single pass — it *should* work end-to-end,
> and the known-untested edges are called out in the
> [caveats section](#windows-and-linux-caveats) below. If something
> breaks on your machine please open an issue with the traceback.

## What you need

- **Python 3.10 or newer.** Check by opening a terminal and running
  `python3 --version` (macOS/Linux) or `python --version` (Windows).
  If it says something older than 3.10, install a newer version from
  [python.org](https://www.python.org/downloads/) or via your package
  manager (`brew install python`, `winget install Python.Python.3.12`,
  `apt install python3`, etc.).
- **An internet connection** for the first run (to fetch card images
  from Scryfall). After that it works offline for anything you've
  already looked at.
- **A printer** that can do full-colour A4 or US Letter output. Duplex
  printing is optional but nice for card backs.

## One-time setup

Pick the section for your OS. The commands are the same everywhere, just
the shell details differ.

<details open>
<summary><b>macOS</b> (Terminal)</summary>

```bash
cd /path/to/RelMTGProxy
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

</details>

<details>
<summary><b>Windows</b> (PowerShell)</summary>

```powershell
cd C:\path\to\RelMTGProxy
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

If PowerShell refuses to run the activation script, allow local scripts
for this user once:
`Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned`.

</details>

<details>
<summary><b>Linux</b> (bash / zsh)</summary>

```bash
cd /path/to/RelMTGProxy
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

</details>

That's it — you can now run the tool.

## Running the app

From the project folder, with the virtual environment activated:

```bash
python cli.py pick
```

That command starts a small local web server and opens
**[http://127.0.0.1:8787](http://127.0.0.1:8787)** in your browser. If it
doesn't open automatically, click that link.

To stop the server, come back to the terminal window and press **Ctrl-C**.

Every new terminal window needs the venv reactivated first:
`source .venv/bin/activate` (macOS/Linux) or
`.\.venv\Scripts\Activate.ps1` (Windows).

## Using the app

Once the browser page opens:

1. **Click `+ New`** in the left-hand Projects panel.
2. **Give the deck a name** (e.g. "Mazirek Sacrifice") and paste in one
   of these:
   - A decklist (from Moxfield, Archidekt, MTGO — plain text with one
     card per line, quantity first: `1 Sol Ring`).
   - A **Moxfield** deck URL like `https://www.moxfield.com/decks/ABC123`.
   - An **Archidekt** deck URL like `https://archidekt.com/decks/15364137`.
   - Or drop card images onto the "Drop card art here" box.
3. **Hit Create project.** RelMTG Proxy resolves each card on Scryfall
   and drops you into the deck view — a grid of every card's chosen art.
4. **Click any card** to open the art picker. Every alternate printing
   Scryfall knows about is shown; click one to swap the art. Filter
   chips (Hide digital / English only / Frame era) narrow the choices.
5. **Drag cards around** in the grid to reorder them — the order maps
   directly to the printed pages.
6. **Choose paper size** (A4 or US Letter) and **resolution** from the
   top toolbar. Defaults are A4 at 600 DPI.
7. **Hit `Export PDF`**. When it finishes, a green download button and
   a thumbnail strip of each page appear at the top. Click a thumb to
   zoom, or the small "PDF" tag next to it to download just that page
   (useful for reprinting one sheet after a paper jam).

Prints are laid out 3×3 per sheet at the correct MTG card size
(63 × 88 mm) with cut guides between each card.

> **Letter users, heads-up:** at the default 3 mm gutter, Letter leaves
> only ~4.7 mm top/bottom margin — most printers will clip. Drop the
> gutter to 0 in the toolbar, or scale to fit, if your printer refuses
> to print that close to the edge.

## Optional: higher-quality prints (600 DPI or 1200 DPI)

Scryfall's images are ~300 DPI at card size. That looks OK; **600 DPI**
looks like a real card, and **1200 DPI** is overkill for anything short
of a high-end photo printer. Turning either on takes a one-time download.

Two options depending on your hardware.

### Option A — universal (Vulkan-based, ~50 MB)

Works on macOS, Windows, and Linux as long as your GPU has a working
Vulkan driver (any modern integrated or discrete GPU does).

```bash
python cli.py setup-upscaler
```

This downloads the right prebuilt Real-ESRGAN binary for your OS. From
then on, every export runs each card through it. First card on an Apple
Silicon Mac is ~25 s; a mid-range Windows/Linux GPU is comparable. The
second export of the same deck is instant — results are cached.

> **No GPU / headless server / older machine?** The binary requires a
> Vulkan-capable GPU. If it errors out, use Option B on CPU or fall back
> to the ungated 300 DPI export.

### Option B — native (PyTorch, ~700 MB)

Faster and higher quality than the Vulkan binary, but heavier to set up.
Which sub-option depends on your GPU:

<details>
<summary><b>Apple Silicon Mac</b> — automatic Metal (MPS)</summary>

```bash
pip install -r requirements-mps.txt          # PyTorch (~600 MB)
python cli.py setup-upscaler --backend mps   # model weights (~64 MB)
```

</details>

<details>
<summary><b>Windows or Linux with an Nvidia GPU</b> — CUDA</summary>

The plain `torch` package on Windows/Linux is CPU-only. Grab a
CUDA-linked build instead (pick the CUDA version that matches your
driver — check `nvidia-smi`):

```bash
# CUDA 12.1 (works with most modern drivers)
pip install torch --index-url https://download.pytorch.org/whl/cu121
pip install numpy
python cli.py setup-upscaler --backend mps
```

The `--backend mps` flag name is historical — the code auto-picks the
best available device, so it'll actually run on CUDA.

</details>

<details>
<summary><b>No GPU / integrated GPU</b> — CPU</summary>

```bash
pip install -r requirements-mps.txt
python cli.py setup-upscaler --backend mps
```

Runs, but a single card takes minutes rather than seconds. Fine for a
one-off deck; Option A is much better for repeated use.

</details>

Once installed, the app picks the faster backend automatically. You can
turn upscaling off per-export via the **"upscale"** checkbox next to the
Export button.

### Managing installed engines

The left sidebar has a **Models** button that opens a small manager
where you can install / remove community upscaler models (like
Ultramix Balanced, tuned specifically for illustrated card art) without
touching the terminal.

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
`assets/mtg_back.png` and it'll be used from then on. You can also
upload multiple backs via the sidebar's **Backs** panel and pick a
different default per project.

If your printer's duplex alignment is slightly off, run:

```bash
python cli.py testpage
```

Duplex-print the resulting `output/registration_test.pdf` on plain
paper, hold it to a lamp, and measure how far the back marks are from
the front marks. Then feed those numbers into the toolbar's "Align" X/Y
inputs at export time (or pass `--back-offset-x` / `--back-offset-y` on
the CLI).

## Where things end up

- `output/` — every PDF you export, timestamped so you keep a history.
- `projects/` — one JSON file per deck, safe to back up.
- `cache/` — downloaded card images, upscaled versions, uploaded art.
  Nothing here is precious; deleting the folder just makes the next
  export slower while things re-cache.
- `vendor/` — the upscaler binary and model weights, downloaded by
  `setup-upscaler`.

## Command-line, for the curious

Everything you can do in the browser you can also do from the terminal:

```bash
python cli.py new decks/mazirek.txt                     # from a decklist file
python cli.py new https://moxfield.com/decks/ABC123     # from a Moxfield URL
python cli.py new https://archidekt.com/decks/15364137  # from an Archidekt URL
python cli.py add "Mazirek Sacrifice" "Sol Ring"        # add one card
python cli.py export "Mazirek Sacrifice"                # render the PDF
python cli.py pick                                       # open the web UI
```

For the full list, `python cli.py --help`.

## Troubleshooting

**"Command not found: python3" / "python is not recognized"** — install
Python from python.org or your OS package manager, close and reopen the
terminal, try again. On Windows make sure the installer's "Add Python
to PATH" checkbox was ticked.

**"No module named …"** — you likely forgot to activate the virtual
environment. Run `source .venv/bin/activate` (macOS/Linux) or
`.\.venv\Scripts\Activate.ps1` (Windows) first.

**Browser doesn't open automatically** — copy
`http://127.0.0.1:8787` into any browser tab.

**Export button spins forever** — check the terminal window. If a card
lookup failed on Scryfall you'll see the error there. Delete the card,
re-add with the exact name, try again.

**Cards look pixelated in the printed PDF** — you probably have
upscaling turned off or the upscaler isn't installed. Check the toolbar's
"upscale" checkbox and run `python cli.py setup-upscaler` if you haven't
yet.

**`setup-upscaler` fails with a Vulkan error on Windows/Linux** — your
GPU driver isn't exposing Vulkan. Update the driver, or use Option B on
CPU (`pip install -r requirements-mps.txt`).

**Moxfield / Archidekt import says "deck not found"** — the deck is
private (login-only) or has been deleted. Only public decks work.

## Windows and Linux caveats

These are the specific things the maintainer hasn't verified on real
hardware. None are known to be broken — they're things worth watching
for:

- **Windows Defender / SmartScreen may flag `realesrgan-ncnn-vulkan.exe`
  on first run.** The binary comes straight from the official
  [Real-ESRGAN GitHub release](https://github.com/xinntao/Real-ESRGAN/releases/tag/v0.2.5.0),
  unsigned. If your AV quarantines it, you'll need to restore it and
  add an exclusion for the `vendor\` folder. Some antivirus products
  will also flag Vulkan-based upscalers as "potentially unwanted"
  regardless of source; use Option B (PyTorch) if that's you.
- **Windows Long Paths.** If your project ends up nested very deep
  (`C:\Users\<long name>\Documents\<...>\RelMTGProxy`), the cache paths
  can approach the classic 260-char limit. Modern Windows 10/11 have a
  Long Paths opt-in — enable it via `gpedit.msc` → *Computer
  Configuration* → *Administrative Templates* → *System* → *Filesystem*
  → *Enable Win32 long paths*, or the equivalent registry key. Or just
  put the project folder near the root of a drive.
- **Vulkan drivers are required for Option A.** Any modern Intel /
  AMD / Nvidia driver ships with Vulkan, but headless VMs, WSL2 without
  GPU passthrough, and some very old integrated GPUs don't. Option B
  (PyTorch) falls back to CPU cleanly in those cases.
- **Case-sensitivity on Linux.** Project names and uploaded filenames
  keep their original case. `MyDeck` and `mydeck` are two different
  projects on Linux and the same one on macOS/Windows. Don't mix
  cases when moving projects between OSes.
- **CRLF vs LF in decklists.** The parser is line-oriented and treats
  either the same, but if you're editing decklists on Windows and
  pasting into the browser, extra trailing whitespace can occasionally
  make Scryfall lookups miss. Strip trailing whitespace if a lookup
  fails on a card you know exists.
- **CUDA setup takes a specific command.** The plain `pip install torch`
  wheel on Windows/Linux is CPU-only. See the
  "[higher-quality prints](#optional-higher-quality-prints-600-dpi-or-1200-dpi)"
  section — it's a one-line change to use a CUDA-linked wheel.
- **Windows console encoding.** The tool prints unicode (mm arrows,
  em-dashes, card names with diacritics). Modern Windows Terminal / PS7
  handles UTF-8 fine; legacy `cmd.exe` sometimes shows `?` marks in the
  logs. Cosmetic only — files on disk are always UTF-8.
- **Server binds to 127.0.0.1 only.** Not a bug — a deliberate choice.
  If you want to expose the UI to another device on your LAN (e.g. a
  tablet), you'll need to add `--host 0.0.0.0` support or run behind a
  reverse proxy. Doing so removes localhost-only protection so only do
  it on a trusted network.

## What's under the hood (specification)

See [`mtg-proxy-studio-spec.md`](./mtg-proxy-studio-spec.md) for the
original design doc that describes every subsystem in detail.
