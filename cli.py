"""MTG Proxy Studio — command-line entry point."""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
from pathlib import Path

from proxy_studio import backs as BK
from proxy_studio import decklist as DL
from proxy_studio import moxfield as MX
from proxy_studio import scryfall as SF
from proxy_studio import upscale as UP
from proxy_studio.layout import PageSpec
from proxy_studio.pdf_export import (
    DEFAULT_CUT_COLOR, RenderCard, default_output_path, parse_hex_color,
    render_pdf, render_registration_test,
)
from proxy_studio.project import Entry, Project, SelectedPrint


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )


# --- Sub-commands -----------------------------------------------------------

def cmd_new(args: argparse.Namespace) -> int:
    """Create a project JSON from a decklist file or Moxfield URL.

    Accepts:
      - a path to a plain-text decklist file
      - a Moxfield URL like https://www.moxfield.com/decks/{id}
      - a bare Moxfield deck ID

    Resolves each entry to Scryfall's canonical card. If the decklist pinned a
    (set, cn), that exact printing is used; otherwise a sensible default is
    chosen. Failures are reported at the end so the user can fix them and
    re-run `python cli.py add`.
    """
    source = args.decklist

    if MX.looks_like_moxfield(source):
        print(f"Importing from Moxfield: {source}")
        try:
            mox_name, entries = MX.fetch_deck(source)
        except MX.MoxfieldError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        project_name = args.name or MX.sanitize_project_name(mox_name)
        print(f"Fetched {len(entries)} unique cards ({mox_name!r}).")
    else:
        deck_path = Path(source)
        if not deck_path.exists():
            print(f"error: decklist file not found: {deck_path}", file=sys.stderr)
            return 2
        project_name = args.name or deck_path.stem
        try:
            entries = DL.parse_file(deck_path)
        except DL.DecklistError as e:
            print(str(e), file=sys.stderr)
            return 2
        print(f"Parsed {len(entries)} unique cards from {deck_path}.")

    client = SF.ScryfallClient()
    project = Project(name=project_name)
    failures: list[tuple[DL.DeckEntry, str]] = []

    for de in entries:
        try:
            card = client.resolve_named(de.name, de.set_code, de.collector_number)
            # If the user didn't pin a printing, keep the resolved default;
            # otherwise this IS the pinned printing.
            project.add_entry(Entry(
                quantity=de.quantity,
                name=card.get("name", de.name),
                oracle_id=card.get("oracle_id", ""),
                selected_print=SelectedPrint(
                    scryfall_id=card["id"],
                    set=card.get("set", ""),
                    collector_number=card.get("collector_number", ""),
                ),
                layout=card.get("layout", "normal"),
                back="face" if card.get("layout") in SF.DFC_LAYOUTS else "standard",
            ))
        except SF.NotFoundError as e:
            failures.append((de, str(e)))
        except SF.ScryfallError as e:
            failures.append((de, f"Scryfall error: {e}"))

    path = project.save()
    print(f"Saved project: {path}")
    if failures:
        print(f"\n{len(failures)} card(s) could not be resolved:", file=sys.stderr)
        for de, msg in failures:
            print(f"  - {de.quantity}x {de.name}: {msg}", file=sys.stderr)
        return 1
    return 0


def cmd_add(args: argparse.Namespace) -> int:
    """Append a single card to an existing project."""
    project = Project.load(args.project)
    client = SF.ScryfallClient()
    try:
        card = client.resolve_named(args.card)
    except SF.NotFoundError as e:
        print(str(e), file=sys.stderr)
        return 2

    project.add_entry(Entry(
        quantity=args.quantity,
        name=card.get("name", args.card),
        oracle_id=card.get("oracle_id", ""),
        selected_print=SelectedPrint(
            scryfall_id=card["id"],
            set=card.get("set", ""),
            collector_number=card.get("collector_number", ""),
        ),
        layout=card.get("layout", "normal"),
        back="face" if card.get("layout") in SF.DFC_LAYOUTS else "standard",
    ))
    path = project.save()
    print(f"Added {args.quantity}x {card.get('name')} to {path}")
    return 0


def cmd_setup_upscaler(args: argparse.Namespace) -> int:
    """Install an upscaler backend into `vendor/`.

    `--backend ncnn` (default): download the realesrgan-ncnn-vulkan macOS
    binary (~50 MB). Runs everywhere via Rosetta on Apple Silicon.

    `--backend mps`: download RealESRGAN_x4plus.pth (~64 MB) for the
    native PyTorch/MPS backend. Requires `pip install torch numpy`.
    """
    if args.backend == "mps":
        return _setup_mps(args)
    return _setup_ncnn(args)


def _setup_mps(args: argparse.Namespace) -> int:
    import urllib.request

    # Sanity-check torch is importable before we download 64 MB of weights.
    try:
        import torch  # noqa: F401
    except ImportError:
        print("error: PyTorch not installed. Run `pip install -r "
              "requirements-mps.txt` (or `pip install torch numpy`) "
              "first.", file=sys.stderr)
        return 2

    UP.MPS_VENDOR_DIR.mkdir(parents=True, exist_ok=True)
    weights = UP.MPS_VENDOR_DIR / UP.MPS_WEIGHTS_FILENAME
    if weights.exists() and not args.force:
        print(f"Weights already at {weights}. Re-run with --force to redownload.")
        return 0

    url = ("https://github.com/xinntao/Real-ESRGAN/releases/"
           "download/v0.1.0/RealESRGAN_x4plus.pth")
    print(f"Downloading {url}")
    try:
        with urllib.request.urlopen(url, timeout=180) as resp:
            data = resp.read()
    except Exception as e:
        print(f"error: download failed: {e}", file=sys.stderr)
        return 2
    tmp = weights.with_suffix(".pth.tmp")
    tmp.write_bytes(data)
    tmp.replace(weights)
    print(f"Wrote {weights} ({len(data) / 1024 / 1024:.1f} MB)")

    print("Self-check…")
    try:
        up = UP.MpsUpscaler()
    except UP.UpscalerNotAvailable as e:
        print(f"warning: {e}", file=sys.stderr)
        return 1
    print(f"OK. Device: {up._resolved_device}")
    return 0


def _setup_ncnn(args: argparse.Namespace) -> int:
    import io
    import shutil
    import stat
    import urllib.request
    import zipfile

    vendor_dir = Path("vendor/realesrgan-ncnn-vulkan")
    binary = vendor_dir / UP.BINARY_NAME
    models_dir = vendor_dir / "models"

    if not args.force and binary.exists() and models_dir.exists():
        print(f"Already installed at {binary}. Re-run with --force to reinstall.")
        return 0

    if args.force and vendor_dir.exists():
        shutil.rmtree(vendor_dir)

    # v0.2.5.0 macOS build bundles the binary AND the ncnn model files
    # (~50 MB). Earlier v0.2.0 only shipped the binary, which is useless
    # without the .bin/.param model files.
    url = ("https://github.com/xinntao/Real-ESRGAN/releases/"
           "download/v0.2.5.0/realesrgan-ncnn-vulkan-20220424-macos.zip")
    print(f"Downloading {url}")
    try:
        with urllib.request.urlopen(url, timeout=180) as resp:
            data = resp.read()
    except Exception as e:
        print(f"error: download failed: {e}", file=sys.stderr)
        return 2
    print(f"Downloaded {len(data) / 1024 / 1024:.1f} MB; extracting…")

    vendor_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        # Some releases nest everything under a single top-level dir, others
        # extract flat. Detect and strip only if a common prefix exists.
        names = [n for n in zf.namelist() if not n.startswith("__MACOSX/")]
        top_dirs = {Path(n).parts[0] for n in names if n}
        strip_prefix = len(top_dirs) == 1 and any(n.endswith("/") for n in names
                                                    if Path(n).parts[0] in top_dirs
                                                    and len(Path(n).parts) == 1)
        for member in zf.infolist():
            if member.filename.startswith("__MACOSX/"):
                continue
            if member.filename.endswith("/.DS_Store"):
                continue
            parts = Path(member.filename).parts
            if not parts:
                continue
            if strip_prefix:
                if len(parts) < 2:
                    continue
                rel = Path(*parts[1:])
            else:
                rel = Path(*parts)
            dst = vendor_dir / rel
            if member.is_dir():
                dst.mkdir(parents=True, exist_ok=True)
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member) as src, dst.open("wb") as fh:
                fh.write(src.read())

    if not binary.exists():
        print(f"error: binary missing after extract: {binary}", file=sys.stderr)
        return 2

    # +x for owner/group/other.
    mode = binary.stat().st_mode
    binary.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    # Strip macOS Gatekeeper quarantine so the user isn't prompted every run.
    try:
        subprocess.run(["xattr", "-dr", "com.apple.quarantine", str(vendor_dir)],
                       check=False, capture_output=True)
    except FileNotFoundError:
        pass

    print(f"Installed to {binary}")
    print("Self-check…")
    try:
        result = subprocess.run(
            [str(binary), "-h"],
            capture_output=True, text=True, timeout=15,
        )
    except Exception as e:
        print(f"warning: could not run binary: {e}", file=sys.stderr)
        return 1

    # realesrgan-ncnn-vulkan prints help to stderr and exits 0 or 255.
    out = (result.stderr or result.stdout).strip().splitlines()
    if not out:
        print("warning: no output from binary — install may still work at runtime.",
              file=sys.stderr)
        return 1
    for line in out[:6]:
        print(f"  {line}")
    print("OK.")
    return 0


def cmd_pick(args: argparse.Namespace) -> int:
    """Launch the art-picker UI in the browser.

    With no project argument, opens the project list. With a project name,
    deep-links via `#project=NAME` in the URL fragment so the UI opens that
    project directly.
    """
    import threading
    import time
    import webbrowser

    import uvicorn

    from proxy_studio.server import create_app

    if args.project:
        # Verify the project exists so we fail fast rather than serving a
        # broken deep-link.
        Project.load(args.project)

    app = create_app()
    frag = f"#project={args.project}" if args.project else ""
    url = f"http://127.0.0.1:{args.port}/{frag}"

    def _open() -> None:
        time.sleep(0.6)
        if not args.no_browser:
            webbrowser.open(url)

    threading.Thread(target=_open, daemon=True).start()
    print(f"Serving picker at {url}  (Ctrl-C to stop)")
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="info")
    return 0


def cmd_upscale(args: argparse.Namespace) -> int:
    """Upscale every front image for a project (idempotent).

    Standalone version of the upscale step so users can pre-warm the cache
    before running an export, or re-upscale after switching selections.
    """
    project = Project.load(args.project)
    client = SF.ScryfallClient()
    try:
        upscaler = UP.select_upscaler(args.backend)
    except UP.UpscalerNotAvailable as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(f"Backend: {type(upscaler).__name__}")

    total = len(project.entries)
    for i, entry in enumerate(project.entries, start=1):
        card = client.resolve_named(entry.name, entry.selected_print.set,
                                    entry.selected_print.collector_number)
        front, _back = client.face_images_for(card)
        src = client.download_image(front)
        print(f"[{i}/{total}] upscaling {entry.name}")
        out = UP.upscale_image(src, front.scryfall_id, front.face_index,
                               scale=args.scale, upscaler=upscaler)
        UP.check_dpi_gate(out, label=entry.name)
    print("Done.")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    """Download images for every selected printing and render a fronts PDF.

    With `--upscale` (default when the binary is installed), each image is
    routed through Real-ESRGAN before rendering, hitting ~600 DPI at 63×88 mm.
    """
    project = Project.load(args.project)
    client = SF.ScryfallClient()

    # Decide upscale mode.
    if args.upscale is None:
        upscale_on = UP.any_backend_installed()
        if not upscale_on:
            print("note: no upscaler backend installed — rendering at "
                  "Scryfall's native resolution. Run `python cli.py "
                  "setup-upscaler` for 600 DPI output.")
    else:
        upscale_on = args.upscale

    upscaler: UP.UpscaleBackend | None = None
    if upscale_on:
        try:
            upscaler = UP.select_upscaler(args.backend)
        except UP.UpscalerNotAvailable as e:
            print(f"error: --upscale set but {e}", file=sys.stderr)
            return 2
        print(f"Backend: {type(upscaler).__name__}")

    render_cards: list[RenderCard] = []
    total = len(project.entries)
    upscale_hits = upscale_misses = 0
    need_backs = args.backs != "none"
    for i, entry in enumerate(project.entries, start=1):
        # Custom uploaded art: skip Scryfall + download. Only upscale if
        # both the toggle is on AND the source image is below the 600 DPI
        # target — re-processing an already-4K user asset would be wasteful.
        if entry.custom_image_path:
            img_path = Path(entry.custom_image_path)
            if not img_path.exists():
                print(f"error: entry {entry.name!r} references missing file "
                      f"{img_path}", file=sys.stderr)
                return 2
            if upscaler is not None and UP.needs_upscale(img_path):
                cache_key = f"custom-{project.name}-{img_path.stem}"
                print(f"[{i}/{total}] upscaling {entry.name} (custom art)")
                img_path = UP.upscale_image(img_path, cache_key, 0,
                                             scale=args.scale, upscaler=upscaler)
            UP.check_dpi_gate(img_path, warn=args.dpi_warn,
                               fail=args.dpi_fail, label=entry.name)
            render_cards.append(RenderCard(image_path=img_path, name=entry.name,
                                            quantity=entry.quantity))
            continue

        card = client.resolve_named(
            entry.name, entry.selected_print.set,
            entry.selected_print.collector_number,
        )
        front, _back = client.face_images_for(card)
        img_path = client.download_image(front)
        if upscaler is not None:
            cached = UP.DEFAULT_CACHE_DIR / (
                f"{front.scryfall_id}_face{front.face_index}_x{args.scale}.png")
            if cached.exists() and cached.stat().st_size > 0:
                upscale_hits += 1
            else:
                print(f"[{i}/{total}] upscaling {entry.name}")
                upscale_misses += 1
            img_path = UP.upscale_image(img_path, front.scryfall_id,
                                        front.face_index,
                                        scale=args.scale,
                                        upscaler=upscaler)
        UP.check_dpi_gate(img_path, warn=args.dpi_warn, fail=args.dpi_fail,
                          label=entry.name)

        back_path = None
        if need_backs:
            try:
                back_path = BK.resolve_back_image(
                    entry, client=client, upscaler=upscaler, scale=args.scale)
            except BK.BackResolutionError as e:
                print(f"error: {e}", file=sys.stderr)
                return 2

        render_cards.append(RenderCard(image_path=img_path, name=entry.name,
                                       quantity=entry.quantity,
                                       back_image_path=back_path))
    if upscaler is not None and upscale_hits:
        print(f"upscale cache: {upscale_hits} hit, {upscale_misses} rebuilt")

    out_path = default_output_path(project.name, args.backs,
                                    timestamp=not args.no_timestamp)

    spec = PageSpec(gutter_mm=args.gutter, cut_line_mode=args.cut_lines)
    try:
        cut_color = parse_hex_color(args.cut_color) if args.cut_color else DEFAULT_CUT_COLOR
    except ValueError as e:
        print(f"error: --cut-color: {e}", file=sys.stderr)
        return 2

    fronts_path, backs_path = render_pdf(
        render_cards, out_path,
        project_name=project.name,
        spec=spec,
        backs_mode=args.backs,
        flip_edge=args.flip_edge,
        back_offset_x_mm=args.back_offset_x,
        back_offset_y_mm=args.back_offset_y,
        cut_color=cut_color,
        dpi_warn=args.dpi_warn,
        dpi_fail=args.dpi_fail,
    )
    print(f"Wrote {fronts_path}")
    if backs_path:
        print(f"Wrote {backs_path}")
    return 0


def cmd_testpage(args: argparse.Namespace) -> int:
    """Emit a duplex registration test PDF.

    Print duplex on plain paper, hold to the light, compare front and back
    marks. Measure any misalignment in mm and pass those numbers to
    `--back-offset-x/-y` on the export command.
    """
    spec = PageSpec()
    out = Path("output") / "registration_test.pdf"
    render_registration_test(
        out, spec=spec,
        flip_edge=args.flip_edge,
        back_offset_x_mm=args.back_offset_x,
        back_offset_y_mm=args.back_offset_y,
    )
    print(f"Wrote {out}")
    print("Duplex-print this on plain paper, hold to the light, and read off "
          "any drift. Feed those millimetres to `export --back-offset-x/-y`.")
    return 0


# --- Argparse wiring --------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="mtg-proxy-studio",
                                description="Local decklist → print-ready PDF pipeline")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_new = sub.add_parser("new",
                            help="Create a project from a decklist file or Moxfield URL")
    p_new.add_argument("decklist",
                        help="Path to a decklist .txt file, a Moxfield URL, "
                             "or a bare Moxfield deck ID")
    p_new.add_argument("--name",
                        help="Project name (default: file stem or Moxfield deck name)")
    p_new.set_defaults(func=cmd_new)

    p_add = sub.add_parser("add", help="Append a card to an existing project")
    p_add.add_argument("project", help="Project name (matches projects/<name>.json)")
    p_add.add_argument("card", help="Card name to add")
    p_add.add_argument("-n", "--quantity", type=int, default=1)
    p_add.set_defaults(func=cmd_add)

    p_pick = sub.add_parser("pick", help="Launch the art-picker web UI")
    p_pick.add_argument("project", nargs="?", default=None,
                        help="Optional: open this project directly. "
                             "Without it, the UI shows the project list.")
    p_pick.add_argument("--port", type=int, default=8787)
    p_pick.add_argument("--no-browser", action="store_true",
                        help="Don't open a browser tab automatically")
    p_pick.set_defaults(func=cmd_pick)

    p_export = sub.add_parser("export", help="Render fronts PDF for a project")
    p_export.add_argument("project")
    p_export.add_argument("--gutter", type=float, default=3.0,
                          help="mm between cards (default 3.0)")
    p_export.add_argument("--cut-lines", choices=["full", "ticks"],
                          default="full", dest="cut_lines",
                          help="'full' = edge-to-edge coloured guides "
                               "(default); 'ticks' = margin-only marks")
    p_export.add_argument("--cut-color", default="#4d8bff", dest="cut_color",
                          help="Hex colour for cut guides (default #4d8bff)")
    upscale_group = p_export.add_mutually_exclusive_group()
    upscale_group.add_argument("--upscale", dest="upscale",
                                action="store_true", default=None,
                                help="Force upscaling on (default: auto-detect binary)")
    upscale_group.add_argument("--no-upscale", dest="upscale",
                                action="store_false",
                                help="Skip upscaling; use raw Scryfall PNGs")
    p_export.add_argument("--scale", type=int, default=UP.DEFAULT_TARGET_SCALE,
                          help=f"Upscale factor (default {UP.DEFAULT_TARGET_SCALE})")
    p_export.add_argument("--backend", choices=["auto", "mps", "ncnn"],
                          default="auto",
                          help="Which upscaler backend to use (default: auto — "
                               "prefer MPS on Apple Silicon, else ncnn)")
    p_export.add_argument("--dpi-warn", type=float, default=550.0,
                          dest="dpi_warn",
                          help="Warn threshold (spec §7 default 550)")
    p_export.add_argument("--dpi-fail", type=float, default=290.0,
                          dest="dpi_fail",
                          help="Hard-fail threshold (spec §7 default 290)")
    p_export.add_argument("--backs", choices=["none", "duplex", "separate"],
                          default="none",
                          help="Include card backs: interleaved (duplex) or "
                               "in a second PDF (separate)")
    p_export.add_argument("--flip-edge", choices=["long", "short"],
                          default="long", dest="flip_edge",
                          help="Duplex flip axis (A4 portrait default: long)")
    p_export.add_argument("--back-offset-x", type=float, default=0.0,
                          dest="back_offset_x",
                          help="mm to shift all back-page content in X to "
                               "compensate for printer drift")
    p_export.add_argument("--back-offset-y", type=float, default=0.0,
                          dest="back_offset_y",
                          help="mm to shift all back-page content in Y")
    p_export.add_argument("--no-timestamp", action="store_true",
                          dest="no_timestamp",
                          help="Skip the YYYY-MM-DD-HHMM suffix on output "
                               "filenames — new exports will overwrite the "
                               "previous one")
    p_export.set_defaults(func=cmd_export)

    p_test = sub.add_parser("testpage",
                             help="Emit a duplex registration test PDF")
    p_test.add_argument("--flip-edge", choices=["long", "short"],
                        default="long", dest="flip_edge")
    p_test.add_argument("--back-offset-x", type=float, default=0.0,
                        dest="back_offset_x")
    p_test.add_argument("--back-offset-y", type=float, default=0.0,
                        dest="back_offset_y")
    p_test.set_defaults(func=cmd_testpage)

    p_setup = sub.add_parser("setup-upscaler",
                              help="Install an upscaler backend (ncnn binary or MPS weights)")
    p_setup.add_argument("--backend", choices=["ncnn", "mps"], default="ncnn",
                         help="'ncnn' = prebuilt binary (universal); 'mps' = "
                              "PyTorch weights for native Apple Silicon speed")
    p_setup.add_argument("--force", action="store_true",
                         help="Reinstall even if already present")
    p_setup.set_defaults(func=cmd_setup_upscaler)

    p_up = sub.add_parser("upscale",
                          help="Pre-warm the upscaled-image cache for a project")
    p_up.add_argument("project")
    p_up.add_argument("--scale", type=int, default=UP.DEFAULT_TARGET_SCALE,
                      help=f"Upscale factor (default {UP.DEFAULT_TARGET_SCALE})")
    p_up.add_argument("--backend", choices=["auto", "mps", "ncnn"],
                      default="auto",
                      help="Which upscaler backend to use (default: auto)")
    p_up.set_defaults(func=cmd_upscale)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _configure_logging(args.verbose)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
