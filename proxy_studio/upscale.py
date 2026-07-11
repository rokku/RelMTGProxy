"""Image upscaling via Real-ESRGAN ncnn-vulkan.

Wraps the prebuilt `realesrgan-ncnn-vulkan` binary (installed by
`python cli.py setup-upscaler`). Idempotent — outputs are keyed by
`{scryfall_id}_face{N}_x{scale}.png` in `cache/images/upscaled/`.

Pipeline follows spec §7: run the `realesrgan-x4plus` model at its native
4× scale, then downsample to 2× with PIL LANCZOS for the effective
`--outscale 2` behaviour. Card art benefits from that pipeline vs a
dedicated 2× model.
"""

from __future__ import annotations

import logging
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from PIL import Image

from .layout import CARD_H_MM, CARD_W_MM

log = logging.getLogger(__name__)

# --- Paths / constants ------------------------------------------------------
DEFAULT_CACHE_DIR = Path("cache/images/upscaled")
DEFAULT_VENDOR_DIR = Path("vendor/realesrgan-ncnn-vulkan")


def _binary_name() -> str:
    """Executable name for the ncnn-vulkan binary on the current OS.

    The prebuilt release ships as `.exe` on Windows and plain executables
    on macOS/Linux. Callers should always go through this rather than
    hardcoding a name so the same code paths work on all three platforms.
    """
    import sys
    return "realesrgan-ncnn-vulkan.exe" if sys.platform == "win32" \
        else "realesrgan-ncnn-vulkan"


# Kept as a module-level constant for callers that treat it as a name;
# always resolved lazily via `_binary_name()` on the current OS.
BINARY_NAME = _binary_name()

MPS_VENDOR_DIR = Path("vendor/mps")

# The x4plus model has a native 4× scale — the ncnn-vulkan binary supports
# -s {2,3,4} but the spec's better-on-card-art recipe is: run at native 4×,
# then downsample to 2× with a proper resampler.
NATIVE_MODEL_SCALE = 4
DEFAULT_TARGET_SCALE = 2

BackendName = str   # "auto" | "mps" | "ncnn"
QualityName = str   # "quality" | "fast"

DEFAULT_QUALITY: QualityName = "quality"


@dataclass(frozen=True)
class ModelSpec:
    """One selectable model — same architecture family, different depths.

    `mps_weights_*` are optional: some community models (Ultramix) ship as
    ncnn `.bin` + `.param` only and have no PyTorch `.pth` equivalent.
    Those entries only run under the ncnn-vulkan backend.

    `ncnn_bin_url` / `ncnn_param_url` point at model files fetched *in
    addition to* the base ncnn-vulkan bundle — set for custom community
    models (Ultramix). Leave them None for models that ship inside the
    upstream v0.2.5.0 bundle (x4plus, x4plus-anime).
    """
    ncnn_name: str            # matches the `-n` arg on the ncnn-vulkan binary
    mps_weights_filename: str | None
    mps_weights_url: str | None
    num_block: int            # RRDBNet depth (23 for x4plus, 6 for x4plus-anime)
    description: str
    ncnn_bin_url: str | None = None
    ncnn_param_url: str | None = None


# Adding a new model is a matter of dropping in another entry: point ncnn_*_url
# at the raw files if it isn't in the upstream Real-ESRGAN bundle, and give it
# a `mps_weights_*` pair if a matching `.pth` exists.
MODELS: dict[str, ModelSpec] = {
    "quality": ModelSpec(
        ncnn_name="realesrgan-x4plus",
        mps_weights_filename="RealESRGAN_x4plus.pth",
        mps_weights_url=("https://github.com/xinntao/Real-ESRGAN/releases/"
                          "download/v0.1.0/RealESRGAN_x4plus.pth"),
        num_block=23,
        description="Best quality (full 23-block RRDBNet).",
    ),
    "fast": ModelSpec(
        ncnn_name="realesrgan-x4plus-anime",
        mps_weights_filename="RealESRGAN_x4plus_anime_6B.pth",
        mps_weights_url=("https://github.com/xinntao/Real-ESRGAN/releases/"
                          "download/v0.2.2.4/RealESRGAN_x4plus_anime_6B.pth"),
        num_block=6,
        description="~4× faster, slight softening of fine detail (6-block RRDBNet).",
    ),
    "ultramix": ModelSpec(
        ncnn_name="ultramix-balanced-4x",
        # No public `.pth` release — Upscayl bundles ncnn files only.
        mps_weights_filename=None,
        mps_weights_url=None,
        num_block=23,
        description=("Ultramix Balanced — Upscayl's community-tuned x4plus, "
                     "often sharper on illustrated card art. (ncnn only.)"),
        ncnn_bin_url=("https://raw.githubusercontent.com/upscayl/upscayl/"
                      "main/resources/models/ultramix-balanced-4x.bin"),
        ncnn_param_url=("https://raw.githubusercontent.com/upscayl/upscayl/"
                        "main/resources/models/ultramix-balanced-4x.param"),
    ),
}

# Kept for backwards compat with callers that pass a raw ncnn model name.
DEFAULT_MODEL = MODELS["quality"].ncnn_name


def cache_key_for(scryfall_id: str, face_index: int, scale: int,
                   quality: QualityName = DEFAULT_QUALITY) -> str:
    """Cache filename for an upscaled image.

    The default quality keeps the legacy `_xN.png` naming so any existing
    cache stays valid; alternative models get a `_{quality}` suffix.
    """
    suffix = "" if quality == DEFAULT_QUALITY else f"_{quality}"
    return f"{scryfall_id}_face{face_index}_x{scale}{suffix}.png"


class UpscalerNotAvailable(RuntimeError):
    """Binary not installed. Run `python cli.py setup-upscaler`."""


class UpscaleBackend(Protocol):
    """Minimum interface for upscalers — swappable for tests + future backends."""
    def upscale(self, src: Path, out: Path, *, scale: int = DEFAULT_TARGET_SCALE) -> Path: ...


# --- Ncnn-Vulkan backend ----------------------------------------------------
@dataclass
class NcnnVulkanUpscaler:
    """Subprocess wrapper around the ncnn-vulkan CLI binary.

    `quality` (or the lower-level `model`) picks the network — "quality"
    = `realesrgan-x4plus`, "fast" = `realesrgan-x4plus-anime`. Both ship
    inside the v0.2.5.0 macOS bundle, so no additional download is needed.
    """
    vendor_dir: Path = DEFAULT_VENDOR_DIR
    quality: QualityName = DEFAULT_QUALITY
    model: str | None = None      # None → derive from `quality`
    _binary_path: Path | None = None
    _models_dir: Path | None = None

    def __post_init__(self) -> None:
        if self.model is None:
            if self.quality not in MODELS:
                raise ValueError(f"Unknown quality {self.quality!r}")
            self.model = MODELS[self.quality].ncnn_name
        self.vendor_dir = Path(self.vendor_dir)
        self._binary_path = self._resolve_binary()
        self._models_dir = self._resolve_models_dir()
        if not self._binary_path.exists():
            raise UpscalerNotAvailable(
                f"realesrgan-ncnn-vulkan not found under {self.vendor_dir}. "
                "Run `python cli.py setup-upscaler` to download it."
            )
        if not self._models_dir.exists():
            raise UpscalerNotAvailable(
                f"models directory not found under {self.vendor_dir}. "
                "Re-run `python cli.py setup-upscaler`."
            )
        # For community models fetched separately, confirm the specific
        # .bin/.param pair is present — the base bundle only ships x4plus
        # and x4plus-anime.
        bin_path = self._models_dir / f"{self.model}.bin"
        param_path = self._models_dir / f"{self.model}.param"
        if not (bin_path.exists() and param_path.exists()):
            raise UpscalerNotAvailable(
                f"ncnn model {self.model!r} missing from {self._models_dir}. "
                f"Run `python cli.py setup-upscaler --model {self.quality}` "
                f"to fetch it."
            )

    def _resolve_binary(self) -> Path:
        # The Real-ESRGAN release zip nests everything under
        # `realesrgan-ncnn-vulkan-<date>-<os>/`; setup-upscaler flattens
        # that, but be forgiving of both layouts on any platform.
        for candidate in [self.vendor_dir / BINARY_NAME]:
            if candidate.exists():
                return candidate
        for sub in self.vendor_dir.glob("realesrgan-ncnn-vulkan-*"):
            nested = sub / BINARY_NAME
            if nested.exists():
                return nested
        return self.vendor_dir / BINARY_NAME

    def _resolve_models_dir(self) -> Path:
        binary_parent = (self._binary_path or self.vendor_dir).parent
        for candidate in (
            binary_parent / "models",
            self.vendor_dir / "models",
        ):
            if candidate.exists():
                return candidate
        return binary_parent / "models"

    def upscale(self, src: Path, out: Path, *, scale: int = DEFAULT_TARGET_SCALE) -> Path:
        """Upscale `src` → `out` at the given target scale.

        Runs the x4plus model at its native 4× scale, then downsamples to
        the requested target using PIL LANCZOS. The 4×-then-downsample
        pipeline consistently sharpens card art better than dedicated 2×
        models in our testing.
        """
        src, out = Path(src), Path(out)
        out.parent.mkdir(parents=True, exist_ok=True)

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            tmp_out = Path(tmp.name)
        try:
            cmd = [
                str(self._binary_path),
                "-i", str(src),
                "-o", str(tmp_out),
                "-n", self.model,
                "-s", str(NATIVE_MODEL_SCALE),
                "-m", str(self._models_dir),
                "-f", "png",
            ]
            log.debug("upscaler: %s", " ".join(cmd))
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
            if proc.returncode != 0:
                raise RuntimeError(
                    f"realesrgan-ncnn-vulkan exited {proc.returncode}: "
                    f"{proc.stderr.strip()[:400]}"
                )

            with Image.open(tmp_out) as im:
                # Downsample 4× output to the requested target scale.
                if scale != NATIVE_MODEL_SCALE:
                    ratio = NATIVE_MODEL_SCALE // scale
                    if NATIVE_MODEL_SCALE % scale != 0:
                        # Fractional downsample — use exact target dims.
                        with Image.open(src) as src_im:
                            target = (src_im.width * scale, src_im.height * scale)
                    else:
                        target = (im.width // ratio, im.height // ratio)
                    im = im.resize(target, Image.LANCZOS)

                params: dict[str, object] = {"format": "PNG", "optimize": False}
                icc = _read_icc(src)
                if icc is not None:
                    params["icc_profile"] = icc
                im.save(out, **params)
        finally:
            tmp_out.unlink(missing_ok=True)
        return out


def _read_icc(path: Path) -> bytes | None:
    with Image.open(path) as im:
        return im.info.get("icc_profile")


# --- MPS / PyTorch backend --------------------------------------------------
@dataclass
class MpsUpscaler:
    """Native Apple Silicon backend via PyTorch + MPS.

    Loads a Real-ESRGAN `.pth` file into an inlined RRDBNet and runs
    inference on the Mac GPU. Two model choices via `quality`:
      - "quality" (default) → RealESRGAN_x4plus.pth, 23 blocks, ~22 s/card.
      - "fast" → RealESRGAN_x4plus_anime_6B.pth, 6 blocks, ~5 s/card.
    """
    quality: QualityName = DEFAULT_QUALITY
    weights_path: Path | None = None    # None → derive from `quality`
    device: str = "auto"                # "auto" → mps > cuda > cpu
    # fp16 roughly doubles throughput on MPS and cuts activation memory
    # in half. Card art at print resolution shows no visible degradation.
    half_precision: bool = True
    _model: object | None = None
    _torch: object | None = None
    _resolved_device: str = ""
    _model_dtype: object | None = None
    _model_spec: ModelSpec | None = None

    def __post_init__(self) -> None:
        if self.quality not in MODELS:
            raise ValueError(f"Unknown quality {self.quality!r}; "
                              f"expected one of {list(MODELS)}")
        self._model_spec = MODELS[self.quality]
        if self._model_spec.mps_weights_filename is None:
            raise UpscalerNotAvailable(
                f"Quality {self.quality!r} has no MPS/PyTorch weights — "
                f"only the ncnn-vulkan backend can run it. Use --backend ncnn."
            )
        if self.weights_path is None:
            self.weights_path = MPS_VENDOR_DIR / self._model_spec.mps_weights_filename
        self.weights_path = Path(self.weights_path)
        if not self.weights_path.exists():
            raise UpscalerNotAvailable(
                f"MPS weights not found at {self.weights_path}. "
                f"Run `python cli.py setup-upscaler --backend mps` "
                f"(or `--model {self.quality}` to fetch just this one)."
            )
        try:
            import torch  # noqa: F401
        except ImportError as e:
            raise UpscalerNotAvailable(
                "PyTorch not installed. `pip install torch` (or "
                "`pip install -r requirements-mps.txt`) to enable the MPS backend."
            ) from e
        self._torch = __import__("torch")
        self._resolved_device = self._resolve_device()
        self._model = self._load_model()

    def _resolve_device(self) -> str:
        torch = self._torch
        if self.device != "auto":
            return self.device
        if torch.backends.mps.is_available():
            return "mps"
        if torch.cuda.is_available():
            return "cuda"
        return "cpu"

    def _load_model(self):
        from .rrdbnet import RRDBNet
        torch = self._torch
        # num_block depends on which model file we're loading — the anime
        # variant is 6 blocks vs the full x4plus's 23.
        model = RRDBNet(num_in_ch=3, num_out_ch=3, num_feat=64,
                        num_block=self._model_spec.num_block,
                        num_grow_ch=32, scale=4)
        state = torch.load(self.weights_path, map_location="cpu",
                           weights_only=True)
        if isinstance(state, dict):
            if "params_ema" in state:
                state = state["params_ema"]
            elif "params" in state:
                state = state["params"]
        model.load_state_dict(state, strict=True)
        model.eval()

        # Precision: fp16 on MPS/cuda, fp32 on cpu (fp16 on CPU is slower).
        use_half = self.half_precision and self._resolved_device != "cpu"
        self._model_dtype = torch.float16 if use_half else torch.float32
        model = model.to(self._resolved_device).to(self._model_dtype)
        return model

    # Starting tile size. 384 with 16-px overlap runs comfortably on the
    # M-series unified-memory budget for fp16 inference; if MPS chokes on
    # a given tile we automatically fall back to 256 for the rest of the
    # session (the smaller size is what we know is universally safe).
    tile_size: int = 384
    tile_pad: int = 16
    _min_tile_size: int = 256

    def upscale(self, src: Path, out: Path, *,
                scale: int = DEFAULT_TARGET_SCALE) -> Path:
        import numpy as np
        torch = self._torch
        src, out = Path(src), Path(out)
        out.parent.mkdir(parents=True, exist_ok=True)

        with Image.open(src) as im:
            im = im.convert("RGB")
            src_w, src_h = im.size
            arr = np.asarray(im, dtype=np.float32) / 255.0        # HWC

        # Input tensor stays on CPU; only per-tile patches move to device.
        tensor = torch.from_numpy(arr).permute(2, 0, 1).unsqueeze(0)  # 1×C×H×W

        with torch.no_grad():
            out_tensor_cpu = self._tile_infer(tensor)

        out_arr = (out_tensor_cpu.clamp(0.0, 1.0)
                    .squeeze(0)
                    .permute(1, 2, 0)
                    .numpy() * 255.0
                    ).round().astype("uint8")
        big = Image.fromarray(out_arr, "RGB")

        # Native model is 4×; downsample to the requested target scale with
        # LANCZOS to preserve detail (spec §7 recipe).
        if scale != NATIVE_MODEL_SCALE:
            target = (src_w * scale, src_h * scale)
            big = big.resize(target, Image.LANCZOS)

        params: dict[str, object] = {"format": "PNG", "optimize": False}
        icc = _read_icc(src)
        if icc is not None:
            params["icc_profile"] = icc
        big.save(out, **params)
        return out


    def _tile_infer(self, img: object) -> object:
        """Tile → run → stitch, with an OOM fallback to a smaller tile size.

        MPS sometimes throws `RuntimeError: MPS backend out of memory` on
        larger tiles; when that happens we halve the tile budget and retry
        the whole image. The fallback sticks for the rest of the session so
        we don't rediscover the ceiling on every card.
        """
        while True:
            try:
                return self._tile_infer_at(img, self.tile_size)
            except RuntimeError as e:
                msg = str(e).lower()
                is_oom = "out of memory" in msg or "mps" in msg and "memory" in msg
                if not is_oom or self.tile_size <= self._min_tile_size:
                    raise
                new_tile = max(self._min_tile_size, self.tile_size - 128)
                log.warning(
                    "MPS upscaler hit an OOM at tile=%d; retrying at tile=%d "
                    "for the rest of the session.", self.tile_size, new_tile,
                )
                self.tile_size = new_tile
                # Free whatever the failed run left resident before retrying.
                if hasattr(self._torch, "mps") and hasattr(self._torch.mps, "empty_cache"):
                    try:
                        self._torch.mps.empty_cache()
                    except Exception:
                        pass

    def _tile_infer_at(self, img: object, tile: int) -> object:
        torch = self._torch
        device = self._resolved_device
        _, c, h, w = img.shape
        pad = self.tile_pad
        scale = NATIVE_MODEL_SCALE

        # CPU-side accumulator — only one tile's worth of activation memory
        # ever lives on the GPU at a time.
        out = torch.empty((1, c, h * scale, w * scale), dtype=img.dtype)

        for y in range(0, h, tile):
            for x in range(0, w, tile):
                y0, y1 = y, min(y + tile, h)
                x0, x1 = x, min(x + tile, w)
                py0, py1 = max(0, y0 - pad), min(h, y1 + pad)
                px0, px1 = max(0, x0 - pad), min(w, x1 + pad)

                patch = (img[:, :, py0:py1, px0:px1]
                          .to(device, dtype=self._model_dtype,
                              non_blocking=True))
                sr = self._model(patch)

                top    = (y0 - py0) * scale
                left   = (x0 - px0) * scale
                bottom = top + (y1 - y0) * scale
                right  = left + (x1 - x0) * scale

                out[:, :, y0*scale:y1*scale, x0*scale:x1*scale] = (
                    sr[:, :, top:bottom, left:right]
                        .to("cpu", dtype=self._torch.float32)
                )
        return out


def mps_weights_installed(vendor_dir: Path = MPS_VENDOR_DIR,
                           *, quality: QualityName = DEFAULT_QUALITY) -> bool:
    spec = MODELS.get(quality) or MODELS[DEFAULT_QUALITY]
    if spec.mps_weights_filename is None:
        return False
    return (Path(vendor_dir) / spec.mps_weights_filename).exists()


def ncnn_model_installed(vendor_dir: Path = DEFAULT_VENDOR_DIR,
                          *, quality: QualityName = DEFAULT_QUALITY) -> bool:
    """True if the ncnn `.bin` + `.param` for `quality` are on disk.

    Base-bundle models (x4plus, x4plus-anime) live under models/ post
    `setup-upscaler`; custom models get installed by the same command.
    """
    spec = MODELS.get(quality) or MODELS[DEFAULT_QUALITY]
    binary_dir = Path(vendor_dir) / "models"
    if not binary_dir.exists():
        return False
    bin_ok = (binary_dir / f"{spec.ncnn_name}.bin").exists()
    param_ok = (binary_dir / f"{spec.ncnn_name}.param").exists()
    return bin_ok and param_ok


def ncnn_model_files(vendor_dir: Path,
                     quality: QualityName) -> tuple[Path, Path] | None:
    """Return `(bin_path, param_path)` for a quality, or None if base-bundle."""
    spec = MODELS.get(quality)
    if spec is None:
        return None
    binary_dir = Path(vendor_dir) / "models"
    return (binary_dir / f"{spec.ncnn_name}.bin",
            binary_dir / f"{spec.ncnn_name}.param")


def download_ncnn_model(quality: QualityName, *,
                        vendor_dir: Path = DEFAULT_VENDOR_DIR,
                        force: bool = False) -> tuple[Path, Path]:
    """Download the community-model ncnn files for `quality`.

    Base-bundle models (x4plus, x4plus-anime) live inside the main
    setup-upscaler zip and can't be installed piecemeal — raises
    `ValueError` for those. Returns `(bin_path, param_path)` on success.
    """
    import urllib.request
    spec = MODELS.get(quality)
    if spec is None:
        raise ValueError(f"unknown quality {quality!r}")
    if not spec.ncnn_bin_url or not spec.ncnn_param_url:
        raise ValueError(
            f"{quality!r} is a base-bundle model; install with "
            "`python cli.py setup-upscaler`"
        )
    models_dir = Path(vendor_dir) / "models"
    models_dir.mkdir(parents=True, exist_ok=True)
    bin_target = models_dir / f"{spec.ncnn_name}.bin"
    param_target = models_dir / f"{spec.ncnn_name}.param"
    if not force and bin_target.exists() and param_target.exists():
        return bin_target, param_target
    for url, target in ((spec.ncnn_bin_url, bin_target),
                         (spec.ncnn_param_url, param_target)):
        with urllib.request.urlopen(url, timeout=180) as resp:
            data = resp.read()
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(target)
    return bin_target, param_target


def download_mps_weights(quality: QualityName, *,
                          vendor_dir: Path = MPS_VENDOR_DIR,
                          force: bool = False) -> Path:
    """Download the PyTorch `.pth` weights for `quality`.

    Raises `ValueError` for models that don't ship MPS weights (Ultramix).
    Returns the on-disk path.
    """
    import urllib.request
    spec = MODELS.get(quality)
    if spec is None:
        raise ValueError(f"unknown quality {quality!r}")
    if not spec.mps_weights_url or not spec.mps_weights_filename:
        raise ValueError(f"{quality!r} has no MPS weights available")
    Path(vendor_dir).mkdir(parents=True, exist_ok=True)
    target = Path(vendor_dir) / spec.mps_weights_filename
    if target.exists() and not force:
        return target
    with urllib.request.urlopen(spec.mps_weights_url, timeout=180) as resp:
        data = resp.read()
    tmp = target.with_suffix(".pth.tmp")
    tmp.write_bytes(data)
    tmp.replace(target)
    return target


def uninstall_ncnn_model(quality: QualityName, *,
                          vendor_dir: Path = DEFAULT_VENDOR_DIR) -> list[Path]:
    """Delete the ncnn `.bin` + `.param` files for `quality`.

    Returns the paths that were removed. Base-bundle models can be removed
    this way too — running `setup-upscaler` will restore them.
    """
    files = ncnn_model_files(vendor_dir, quality)
    if files is None:
        raise ValueError(f"unknown quality {quality!r}")
    removed: list[Path] = []
    for p in files:
        if p.exists():
            p.unlink()
            removed.append(p)
    return removed


def uninstall_mps_weights(quality: QualityName, *,
                           vendor_dir: Path = MPS_VENDOR_DIR) -> Path | None:
    """Delete the `.pth` weights for `quality`; returns the removed path."""
    spec = MODELS.get(quality)
    if spec is None:
        raise ValueError(f"unknown quality {quality!r}")
    if not spec.mps_weights_filename:
        return None
    target = Path(vendor_dir) / spec.mps_weights_filename
    if target.exists():
        target.unlink()
        return target
    return None


def torch_mps_available() -> bool:
    """True if `torch` importable AND MPS is functional on this machine."""
    try:
        import torch
    except ImportError:
        return False
    try:
        return bool(torch.backends.mps.is_available())
    except Exception:
        return False


# --- High-level facade ------------------------------------------------------
def upscale_image(src: Path, scryfall_id: str, face_index: int, *,
                  scale: int = DEFAULT_TARGET_SCALE,
                  quality: QualityName = DEFAULT_QUALITY,
                  cache_dir: Path = DEFAULT_CACHE_DIR,
                  upscaler: UpscaleBackend | None = None) -> Path:
    """Return the cached upscaled image path, creating it if missing.

    Idempotent: an existing non-empty output at the cache path is returned
    without re-running the backend. `quality` is included in the cache key
    so alternate models don't stomp each other's outputs. Callers can
    inject `upscaler` to swap the backend (used by tests + backend choice).
    """
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    out = cache_dir / cache_key_for(scryfall_id, face_index, scale, quality)
    if out.exists() and out.stat().st_size > 0:
        return out

    if upscaler is None:
        upscaler = select_upscaler("auto", quality=quality)

    tmp = out.with_suffix(out.suffix + ".tmp")
    upscaler.upscale(Path(src), tmp, scale=scale)
    tmp.replace(out)
    return out


# --- DPI gate --------------------------------------------------------------
def needs_upscale(image_path: Path, *,
                   min_dpi: float = 550.0,
                   card_w_mm: float = CARD_W_MM,
                   card_h_mm: float = CARD_H_MM) -> bool:
    """True if an image is below the 600-DPI-ish target and would benefit
    from a pass through the upscaler.

    A custom image the user already exported from another 600 DPI pipeline
    doesn't need to be re-processed — that would waste time and inflate the
    output. Only images below `min_dpi` (either axis) get upscaled.
    """
    with Image.open(image_path) as im:
        w, h = im.size
    inch = 25.4
    dpi_x = w * inch / card_w_mm
    dpi_y = h * inch / card_h_mm
    return min(dpi_x, dpi_y) < min_dpi


def effective_dpi_of(image_path: Path,
                     card_w_mm: float = CARD_W_MM,
                     card_h_mm: float = CARD_H_MM) -> tuple[float, float]:
    """Effective print DPI for an image if it's rendered at card size."""
    with Image.open(image_path) as im:
        w, h = im.size
    inch = 25.4
    return (w * inch / card_w_mm, h * inch / card_h_mm)


def check_dpi_gate(image_path: Path, *,
                   warn: float = 550.0,
                   fail: float = 290.0,
                   card_w_mm: float = CARD_W_MM,
                   card_h_mm: float = CARD_H_MM,
                   label: str | None = None) -> tuple[float, float]:
    """Log a warning below `warn` DPI; raise below `fail` DPI.

    Central chokepoint for the spec §7 gate — every path (CLI export, picker
    export, standalone `upscale` command) should call this, so the render
    can never silently ship low-DPI cards.
    """
    dpi_x, dpi_y = effective_dpi_of(image_path, card_w_mm, card_h_mm)
    lowest = min(dpi_x, dpi_y)
    tag = label or image_path.name
    if lowest < fail:
        raise ValueError(
            f"Effective DPI {lowest:.0f} for {tag!r} is below hard-fail "
            f"threshold {fail:.0f}. Source image is likely wrong resolution."
        )
    if lowest < warn:
        log.warning("DPI %.0f for %s below warn threshold %.0f",
                    lowest, tag, warn)
    return dpi_x, dpi_y


# --- Backend selection -----------------------------------------------------
def any_backend_installed() -> bool:
    """Cheap check — is at least one backend ready to run?"""
    return is_binary_installed() or mps_weights_installed()


def select_upscaler(backend: BackendName = "auto",
                     *, quality: QualityName = DEFAULT_QUALITY) -> UpscaleBackend:
    """Instantiate the best available upscaler for the requested quality.

    Priority when `backend == "auto"`:
      1. MPS (fastest on Apple Silicon — native, no Rosetta)
      2. ncnn-vulkan (works everywhere via the prebuilt binary)

    Explicit `mps` / `ncnn` forces that backend or raises if unavailable.
    """
    if backend == "mps":
        return MpsUpscaler(quality=quality)
    if backend == "ncnn":
        return NcnnVulkanUpscaler(quality=quality)
    if backend != "auto":
        raise ValueError(f"Unknown backend {backend!r} (expected auto|mps|ncnn)")

    # auto — prefer MPS if the specific weights for `quality` are on disk.
    if torch_mps_available() and mps_weights_installed(quality=quality):
        try:
            up = MpsUpscaler(quality=quality)
            log.info("Using MPS upscaler backend (quality=%s).", quality)
            return up
        except UpscalerNotAvailable as e:
            log.warning("MPS backend unavailable, falling back: %s", e)
    if is_binary_installed():
        log.info("Using ncnn-vulkan upscaler backend (quality=%s).", quality)
        return NcnnVulkanUpscaler(quality=quality)
    raise UpscalerNotAvailable(
        "No upscaler backend installed. Run `python cli.py setup-upscaler` "
        "(ncnn) or `python cli.py setup-upscaler --backend mps` (PyTorch/MPS)."
    )


# --- Small utilities -------------------------------------------------------
def is_binary_installed(vendor_dir: Path = DEFAULT_VENDOR_DIR) -> bool:
    """Cheap check without instantiating the upscaler."""
    return (Path(vendor_dir) / BINARY_NAME).exists()
