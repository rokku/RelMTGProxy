"""MTG Proxy Studio — command-line entry point."""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
from pathlib import Path

from proxy_studio import archidekt as AK
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
    """Create a project JSON from a decklist file or online deck URL.

    Accepts:
      - a path to a plain-text decklist file
      - a Moxfield URL like https://www.moxfield.com/decks/{id}
      - an Archidekt URL like https://archidekt.com/decks/{id}[/slug]
      - a bare Moxfield deck ID or numeric Archidekt deck ID

    Resolves each entry to Scryfall's canonical card. If the decklist pinned a
    (set, cn), that exact printing is used; otherwise a sensible default is
    chosen. Failures are reported at the end so the user can fix them and
    re-run `python cli.py add`.
    """
    source = args.decklist

    # Archidekt is checked first so that a bare numeric deck ID (which also
    # fits Moxfield's NanoID shape at ≥10 chars) is routed to Archidekt; the
    # two URL patterns are disjoint so URLs aren't affected by the order.
    if AK.looks_like_archidekt(source):
        print(f"Importing from Archidekt: {source}")
        try:
            ak_name, entries = AK.fetch_deck(source)
        except AK.ArchidektError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        project_name = args.name or AK.sanitize_project_name(ak_name)
        print(f"Fetched {len(entries)} unique cards ({ak_name!r}).")
    elif MX.looks_like_moxfield(source):
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

    # Sanity-check torch is importable before we download any weights.
    try:
        import torch  # noqa: F401
    except ImportError:
        print("error: PyTorch not installed. Run `pip install -r "
              "requirements-mps.txt` (or `pip install torch numpy`) "
              "first.", file=sys.stderr)
        return 2

    UP.MPS_VENDOR_DIR.mkdir(parents=True, exist_ok=True)

    # Pick which model files to fetch. Default: both, so the Speed/Quality
    # toggle in the UI Just Works right after `setup-upscaler` completes.
    if args.model == "all":
        wanted = list(UP.MODELS.keys())
    elif args.model in UP.MODELS:
        wanted = [args.model]
    else:
        print(f"error: unknown --model {args.model!r}. "
              f"Choose one of: {list(UP.MODELS)}, or 'all'.", file=sys.stderr)
        return 2

    for quality in wanted:
        spec = UP.MODELS[quality]
        target = UP.MPS_VENDOR_DIR / spec.mps_weights_filename
        if target.exists() and not args.force:
            print(f"[{quality}] already at {target}")
            continue
        print(f"[{quality}] downloading {spec.mps_weights_url}")
        try:
            with urllib.request.urlopen(spec.mps_weights_url, timeout=180) as resp:
                data = resp.read()
        except Exception as e:
            print(f"error: download failed: {e}", file=sys.stderr)
            return 2
        tmp = target.with_suffix(".pth.tmp")
        tmp.write_bytes(data)
        tmp.replace(target)
        print(f"[{quality}] wrote {target} ({len(data) / 1024 / 1024:.1f} MB)")

    print("Self-check (quality)…")
    try:
        up = UP.MpsUpscaler(quality="quality")
    except UP.UpscalerNotAvailable as e:
        print(f"warning: {e}", file=sys.stderr)
        return 1
    print(f"OK. Device: {up._resolved_device}")
    return 0


_NCNN_RELEASE_ZIPS = {
    # sys.platform → GitHub release asset filename.
    "darwin": "realesrgan-ncnn-vulkan-20220424-macos.zip",
    "linux":  "realesrgan-ncnn-vulkan-20220424-ubuntu.zip",
    "win32":  "realesrgan-ncnn-vulkan-20220424-windows.zip",
}
_NCNN_RELEASE_BASE = ("https://github.com/xinntao/Real-ESRGAN/releases/"
                       "download/v0.2.5.0/")


def _setup_ncnn(args: argparse.Namespace) -> int:
    import io
    import platform
    import shutil
    import stat
    import urllib.request
    import zipfile

    zip_name = _NCNN_RELEASE_ZIPS.get(sys.platform)
    if zip_name is None:
        print(f"error: no prebuilt Real-ESRGAN binary for platform "
              f"{sys.platform!r} ({platform.system()}). Supported: macOS, "
              "Linux, Windows.", file=sys.stderr)
        return 2

    vendor_dir = Path("vendor/realesrgan-ncnn-vulkan")
    binary = vendor_dir / UP.BINARY_NAME
    models_dir = vendor_dir / "models"

    # Base bundle is already there → skip the download step but still let
    # `_install_custom_ncnn_models` add any newly-declared community models.
    base_installed = binary.exists() and models_dir.exists()
    if base_installed and not args.force:
        print(f"Base binary already installed at {binary}. "
              "Checking custom models…")
        return _install_custom_ncnn_models(vendor_dir, args)

    if args.force and vendor_dir.exists():
        shutil.rmtree(vendor_dir)

    # v0.2.5.0 bundles the binary AND the ncnn model files (~50 MB per OS).
    # Earlier v0.2.0 only shipped the binary, which is useless without the
    # .bin/.param model files.
    url = _NCNN_RELEASE_BASE + zip_name
    print(f"Downloading {url}")
    try:
        with urllib.request.urlopen(url, timeout=180) as resp:
            data = resp.read()
    except Exception as e:
        print(f"error: download failed: {e}", file=sys.stderr)
        return 2
    print(f"Downloaded {len(data) / 1024 / 1024:.1f} MB; extracting…")

    vendor_dir.mkdir(parents=True, exist_ok=True)
    # Guard against Zip Slip: even though we trust the GitHub release, a
    # malformed archive with `../` entries would otherwise let a file land
    # outside `vendor_dir`. Resolve `vendor_dir` once so the containment
    # check compares real paths (macOS symlinks the tmpdir under /var).
    vendor_root = vendor_dir.resolve()
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
            # Refuse anything that resolves outside vendor_dir — blocks
            # `..` traversal and absolute paths embedded in the archive.
            try:
                dst.resolve().relative_to(vendor_root)
            except ValueError:
                print(f"error: refusing to extract member outside vendor "
                      f"dir: {member.filename!r}", file=sys.stderr)
                return 2
            if member.is_dir():
                dst.mkdir(parents=True, exist_ok=True)
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member) as src, dst.open("wb") as fh:
                fh.write(src.read())

    if not binary.exists():
        print(f"error: binary missing after extract: {binary}", file=sys.stderr)
        return 2

    # On macOS/Linux mark the binary executable. Windows uses .exe and needs
    # no chmod; skipping the call also avoids a spurious mode change.
    if sys.platform != "win32":
        mode = binary.stat().st_mode
        binary.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    # Strip macOS Gatekeeper quarantine so the user isn't prompted every run.
    # No-op on other platforms (xattr doesn't exist there).
    if sys.platform == "darwin":
        try:
            subprocess.run(
                ["xattr", "-dr", "com.apple.quarantine", str(vendor_dir)],
                check=False, capture_output=True,
            )
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
    return _install_custom_ncnn_models(vendor_dir, args)


def _install_custom_ncnn_models(vendor_dir: Path,
                                 args: argparse.Namespace) -> int:
    """Fetch community-model `.bin`/`.param` pairs into `models/`.

    Iterates `UP.MODELS` and downloads any entry whose ncnn_bin_url is set
    (base-bundle models leave these None). Honours `--model NAME` to scope
    to a single quality, and `--force` to redownload existing files.
    """
    import urllib.request

    models_dir = vendor_dir / "models"
    models_dir.mkdir(parents=True, exist_ok=True)

    if args.model == "all":
        wanted = list(UP.MODELS.keys())
    elif args.model in UP.MODELS:
        wanted = [args.model]
    else:
        print(f"error: unknown --model {args.model!r}. "
              f"Choose one of: {list(UP.MODELS)}, or 'all'.", file=sys.stderr)
        return 2

    installed_any = False
    for quality in wanted:
        spec = UP.MODELS[quality]
        if not spec.ncnn_bin_url or not spec.ncnn_param_url:
            # Base-bundle model — already extracted from the main zip.
            continue
        bin_target = models_dir / f"{spec.ncnn_name}.bin"
        param_target = models_dir / f"{spec.ncnn_name}.param"
        already = bin_target.exists() and param_target.exists()
        if already and not args.force:
            print(f"[{quality}] already at {bin_target}")
            continue
        for url, target in ((spec.ncnn_bin_url, bin_target),
                             (spec.ncnn_param_url, param_target)):
            print(f"[{quality}] downloading {url}")
            try:
                with urllib.request.urlopen(url, timeout=180) as resp:
                    data = resp.read()
            except Exception as e:
                print(f"error: download failed: {e}", file=sys.stderr)
                return 2
            tmp = target.with_suffix(target.suffix + ".tmp")
            tmp.write_bytes(data)
            tmp.replace(target)
            print(f"[{quality}] wrote {target} "
                  f"({len(data) / 1024 / 1024:.1f} MB)")
        installed_any = True

    if not installed_any and args.model != "all":
        # If the user asked specifically for a base-bundle model, treat as OK.
        spec = UP.MODELS.get(args.model)
        if spec is not None and not spec.ncnn_bin_url:
            print(f"[{args.model}] shipped with the base ncnn bundle — "
                  "nothing extra to fetch.")
    return 0


def cmd_pick(args: argparse.Namespace) -> int:
    """Launch the art-picker UI in the browser.

    With no project argument, opens the project list. With a project name,
    deep-links via `#project=NAME` in the URL fragment so the UI opens that
    project directly.
    """
    import faulthandler
    import threading
    import time
    import webbrowser

    import uvicorn

    from proxy_studio.server import create_app

    # If the process aborts from a native crash (segfault / malloc
    # error), dump every thread's Python stack to stderr so we can
    # tell WHERE the crash came from rather than just the abort code.
    faulthandler.enable()

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
        upscaler = UP.select_upscaler(args.backend, quality=args.quality)
    except UP.UpscalerNotAvailable as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(f"Backend: {type(upscaler).__name__} (quality={args.quality})")

    total = len(project.entries)
    for i, entry in enumerate(project.entries, start=1):
        card = client.resolve_named(entry.name, entry.selected_print.set,
                                    entry.selected_print.collector_number)
        front, _back = client.face_images_for(card)
        src = client.download_image(front)
        print(f"[{i}/{total}] upscaling {entry.name}")
        out = UP.upscale_image(src, front.scryfall_id, front.face_index,
                               scale=args.scale, quality=args.quality,
                               upscaler=upscaler)
        UP.check_dpi_gate(out, label=entry.name)
    print("Done.")
    return 0


def cmd_upscale_test(args: argparse.Namespace) -> int:
    """Run every registered upscaler on one card at both 1200 and 600 DPI.

    Handy for eyeballing which model looks best on your printer before you
    commit to a global default. Outputs land in `output/upscale-tests/<id>/`
    named `<quality>_1200dpi.png` and `<quality>_600dpi.png`.
    """
    client = SF.ScryfallClient()
    # Accept either a raw Scryfall id or a card name.
    if _looks_like_scryfall_id(args.card):
        card = client._get_json(f"{SF.API_BASE}/cards/{args.card}")
        if card.get("__http_status") == 404:
            print(f"error: card {args.card!r} not found", file=sys.stderr)
            return 2
    else:
        try:
            card = client.resolve_named(args.card)
        except SF.NotFoundError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2

    front, _back = client.face_images_for(card)
    src = client.download_image(front)

    from PIL import Image
    out_dir = Path("output/upscale-tests") / card["id"]
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Card: {card.get('name')} ({card.get('set','?').upper()} "
          f"{card.get('collector_number','?')})")
    print(f"Source: {src}")
    print(f"Output dir: {out_dir}")

    for quality, spec in UP.MODELS.items():
        print(f"\n[{quality}] {spec.description}")
        try:
            up = UP.select_upscaler(args.backend, quality=quality)
        except UP.UpscalerNotAvailable as e:
            print(f"  skipped — {e}")
            continue
        out_1200 = out_dir / f"{quality}_1200dpi.png"
        print(f"  → {out_1200.name} (native 4×)")
        try:
            up.upscale(src, out_1200, scale=4)
        except Exception as e:
            print(f"  error: {e}", file=sys.stderr)
            continue
        dpi_x, dpi_y = UP.effective_dpi_of(out_1200)
        print(f"    {out_1200.stat().st_size / 1024:.0f} KB, "
              f"{dpi_x:.0f}×{dpi_y:.0f} DPI")

        out_600 = out_dir / f"{quality}_600dpi.png"
        print(f"  → {out_600.name} (downsample to 2×)")
        with Image.open(src) as orig:
            target = (orig.width * 2, orig.height * 2)
        with Image.open(out_1200) as im:
            icc = im.info.get("icc_profile")
            im = im.resize(target, Image.LANCZOS)
            params: dict[str, object] = {"format": "PNG", "optimize": False}
            if icc is not None:
                params["icc_profile"] = icc
            im.save(out_600, **params)
        dpi_x, dpi_y = UP.effective_dpi_of(out_600)
        print(f"    {out_600.stat().st_size / 1024:.0f} KB, "
              f"{dpi_x:.0f}×{dpi_y:.0f} DPI")

    print(f"\nDone. Compare files under {out_dir}")
    return 0


def _looks_like_scryfall_id(s: str) -> bool:
    # Scryfall ids are UUID-v4 lowercase-hex.
    import re
    return bool(re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        s.strip().lower()))


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
            upscaler = UP.select_upscaler(args.backend, quality=args.quality)
        except UP.UpscalerNotAvailable as e:
            print(f"error: --upscale set but {e}", file=sys.stderr)
            return 2
        print(f"Backend: {type(upscaler).__name__} (quality={args.quality})")

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
                                             scale=args.scale,
                                             quality=args.quality,
                                             upscaler=upscaler)
            UP.check_dpi_gate(img_path, warn=args.dpi_warn,
                               fail=args.dpi_fail, label=entry.name)
            # Custom entries still need a back image resolved when the user
            # asked for duplex/separate — falls back to the standard back
            # (or the project's chosen library back, once that ships below).
            custom_back = None
            if need_backs:
                try:
                    custom_back = BK.resolve_back_image(
                        entry, client=client, upscaler=upscaler,
                        scale=args.scale,
                        library_filename=project.default_back_filename)
                except BK.BackResolutionError as e:
                    print(f"error: {e}", file=sys.stderr)
                    return 2
            render_cards.append(RenderCard(image_path=img_path, name=entry.name,
                                            quantity=entry.quantity,
                                            back_image_path=custom_back))
            continue

        card = client.resolve_named(
            entry.name, entry.selected_print.set,
            entry.selected_print.collector_number,
        )
        front, _back = client.face_images_for(card)
        img_path = client.download_image(front)
        if upscaler is not None:
            cached = UP.DEFAULT_CACHE_DIR / UP.cache_key_for(
                front.scryfall_id, front.face_index, args.scale, args.quality)
            if cached.exists() and cached.stat().st_size > 0:
                upscale_hits += 1
            else:
                print(f"[{i}/{total}] upscaling {entry.name}")
                upscale_misses += 1
            img_path = UP.upscale_image(img_path, front.scryfall_id,
                                        front.face_index,
                                        scale=args.scale,
                                        quality=args.quality,
                                        upscaler=upscaler)
        UP.check_dpi_gate(img_path, warn=args.dpi_warn, fail=args.dpi_fail,
                          label=entry.name)

        back_path = None
        if need_backs:
            try:
                back_path = BK.resolve_back_image(
                    entry, client=client, upscaler=upscaler, scale=args.scale,
                    library_filename=project.default_back_filename)
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
    p_export.add_argument("--quality", choices=list(UP.MODELS),
                          default="quality",
                          help="'quality' = x4plus (default); 'fast' = "
                               "x4plus-anime (~4× faster, subtle detail loss); "
                               "'ultramix' = Upscayl's Ultramix Balanced "
                               "(ncnn only; run setup-upscaler --model "
                               "ultramix first)")
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
    p_setup.add_argument("--model", default="all",
                         help="Which model to install: 'quality' (x4plus), "
                              "'fast' (x4plus-anime), 'ultramix' (Upscayl "
                              "Balanced), or 'all' (default). x4plus and "
                              "x4plus-anime ship in the base ncnn bundle; "
                              "ultramix is fetched separately.")
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
    p_up.add_argument("--quality", choices=list(UP.MODELS), default="quality",
                      help="'quality' (default), 'fast', or 'ultramix' — "
                           "see `export --help`")
    p_up.set_defaults(func=cmd_upscale)

    p_ut = sub.add_parser("upscale-test",
                          help="Run every upscaler on a single card at 1200 "
                               "and 600 DPI so you can compare outputs")
    p_ut.add_argument("card",
                      help="Card name (fuzzy) or Scryfall card id")
    p_ut.add_argument("--backend", choices=["auto", "mps", "ncnn"],
                      default="auto",
                      help="Which upscaler backend to use (default: auto)")
    p_ut.set_defaults(func=cmd_upscale_test)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _configure_logging(args.verbose)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
