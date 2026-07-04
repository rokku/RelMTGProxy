"""Back-face image resolution.

Two paths per spec §8:

- `entry.back == "face"` — a DFC (transform / MDFC): the back is the printing's
  card_faces[1] image, downloaded and upscaled through the same pipeline as
  the front.

- `entry.back == "standard"` — the classic brown MTG card back. Spec says:
  "ship one in `assets/`; source a clean ~600 DPI scan — flag to the user
  that they should supply/approve this asset, don't fabricate provenance."

We do not ship one. If `assets/mtg_back.png` exists we use it; if not we
generate a clearly-labelled placeholder into `cache/` and print a warning
telling the user where to put a real scan.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from . import scryfall as SF
from . import upscale as UP

log = logging.getLogger(__name__)

DEFAULT_ASSETS_DIR = Path("assets")
STANDARD_BACK_FILENAME = "mtg_back.png"
PLACEHOLDER_PATH = Path("cache/images/original/_placeholder_back.png")

# Target pixel dims for a placeholder ~600 DPI at 63×88 mm — matches the
# upscaled front pipeline so the DPI gate passes without re-upscaling.
_PLACEHOLDER_W = 1490
_PLACEHOLDER_H = 2080

_WARN_SUPPLIED_ONCE = False


class BackResolutionError(RuntimeError):
    pass


def get_standard_back(assets_dir: Path = DEFAULT_ASSETS_DIR) -> Path:
    """Return the image path for the standard MTG card back.

    Prefers `assets/mtg_back.png`. Falls back to a generated placeholder in
    `cache/` with a one-time warning explaining how to supply the real image.
    """
    real = Path(assets_dir) / STANDARD_BACK_FILENAME
    if real.exists() and real.stat().st_size > 0:
        return real

    global _WARN_SUPPLIED_ONCE
    if not _WARN_SUPPLIED_ONCE:
        log.warning(
            "No standard MTG back image found at %s. Using a generated "
            "placeholder. Supply a clean ~600 DPI scan you have rights to "
            "use at that path to replace it.", real,
        )
        _WARN_SUPPLIED_ONCE = True

    PLACEHOLDER_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not PLACEHOLDER_PATH.exists() or PLACEHOLDER_PATH.stat().st_size == 0:
        _write_placeholder(PLACEHOLDER_PATH)
    return PLACEHOLDER_PATH


def _write_placeholder(path: Path) -> None:
    """Draw a card-back-shaped placeholder with a clear provenance notice."""
    # Warm brown-ish base like a card back, but obviously artificial.
    im = Image.new("RGB", (_PLACEHOLDER_W, _PLACEHOLDER_H), color=(74, 41, 27))
    draw = ImageDraw.Draw(im)

    # Inner frame — mimics a card back's outer border without imitating art.
    margin = 60
    draw.rounded_rectangle(
        (margin, margin, _PLACEHOLDER_W - margin, _PLACEHOLDER_H - margin),
        radius=90, outline=(200, 160, 90), width=8,
    )
    inner = margin + 40
    draw.rounded_rectangle(
        (inner, inner, _PLACEHOLDER_W - inner, _PLACEHOLDER_H - inner),
        radius=70, outline=(140, 100, 60), width=4,
    )

    # Notice text. Fall back to the default bitmap font if truetype fonts
    # aren't available on the host.
    try:
        font_lg = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 80)
        font_md = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 40)
    except OSError:
        font_lg = ImageFont.load_default()
        font_md = ImageFont.load_default()

    lines = [
        ("PLACEHOLDER", font_lg, (240, 210, 170)),
        ("Standard MTG card back", font_md, (220, 190, 150)),
        ("", font_md, (0, 0, 0)),
        ("Supply your own scan at:", font_md, (200, 160, 110)),
        (f"assets/{STANDARD_BACK_FILENAME}", font_md, (255, 220, 160)),
    ]
    y = _PLACEHOLDER_H // 2 - 200
    for text, font, color in lines:
        w = draw.textlength(text, font=font)
        draw.text((_PLACEHOLDER_W // 2 - w // 2, y), text, fill=color, font=font)
        y += 100 if font is font_lg else 60

    path.parent.mkdir(parents=True, exist_ok=True)
    im.save(path, "PNG")


def resolve_back_image(entry, *, client: SF.ScryfallClient,
                        upscaler: UP.UpscaleBackend | None = None,
                        scale: int = UP.DEFAULT_TARGET_SCALE,
                        assets_dir: Path = DEFAULT_ASSETS_DIR) -> Path:
    """Return the image path to use as the back face for this deck entry.

    - `back == "face"`: uses card_faces[1] from Scryfall for the selected
      printing; upscaled if an upscaler is provided.
    - `back == "standard"`: bundled standard back (or placeholder).
    """
    if entry.back == "standard":
        return get_standard_back(assets_dir)

    if entry.back != "face":
        raise BackResolutionError(f"Unknown back mode {entry.back!r}")

    # DFC path — fetch the printing, get face 1, download + optionally upscale.
    card = client.resolve_named(
        entry.name,
        entry.selected_print.set,
        entry.selected_print.collector_number,
    )
    _front, back_face = client.face_images_for(card)
    if back_face is None:
        raise BackResolutionError(
            f"Entry {entry.name!r} is set to back='face' but the printing "
            f"({entry.selected_print.set} {entry.selected_print.collector_number}) "
            f"is a single-faced layout."
        )
    src = client.download_image(back_face)
    if upscaler is None:
        return src
    return UP.upscale_image(src, back_face.scryfall_id, back_face.face_index,
                             scale=scale, upscaler=upscaler)
