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
BINARY_NAME = "realesrgan-ncnn-vulkan"
DEFAULT_MODEL = "realesrgan-x4plus"

MPS_VENDOR_DIR = Path("vendor/mps")
MPS_WEIGHTS_FILENAME = "RealESRGAN_x4plus.pth"

# The x4plus model has a native 4× scale — the ncnn-vulkan binary supports
# -s {2,3,4} but the spec's better-on-card-art recipe is: run at native 4×,
# then downsample to 2× with a proper resampler.
NATIVE_MODEL_SCALE = 4
DEFAULT_TARGET_SCALE = 2

BackendName = str  # "auto" | "mps" | "ncnn"


class UpscalerNotAvailable(RuntimeError):
    """Binary not installed. Run `python cli.py setup-upscaler`."""


class UpscaleBackend(Protocol):
    """Minimum interface for upscalers — swappable for tests + future backends."""
    def upscale(self, src: Path, out: Path, *, scale: int = DEFAULT_TARGET_SCALE) -> Path: ...


# --- Ncnn-Vulkan backend ----------------------------------------------------
@dataclass
class NcnnVulkanUpscaler:
    """Subprocess wrapper around the ncnn-vulkan CLI binary."""
    vendor_dir: Path = DEFAULT_VENDOR_DIR
    model: str = DEFAULT_MODEL
    _binary_path: Path | None = None
    _models_dir: Path | None = None

    def __post_init__(self) -> None:
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

    def _resolve_binary(self) -> Path:
        # The zip nests everything under `realesrgan-ncnn-vulkan-v0.2.0-macos/`;
        # setup-upscaler strips that, but be forgiving either way.
        for candidate in (
            self.vendor_dir / BINARY_NAME,
            self.vendor_dir / "realesrgan-ncnn-vulkan-v0.2.0-macos" / BINARY_NAME,
        ):
            if candidate.exists():
                return candidate
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

    Loads the `RealESRGAN_x4plus.pth` weights into an inlined RRDBNet and
    runs inference on the Mac GPU. Faster than the Rosetta-emulated
    ncnn-vulkan binary — typically 2–3× on M-series chips.
    """
    weights_path: Path = MPS_VENDOR_DIR / MPS_WEIGHTS_FILENAME
    device: str = "auto"      # "auto" resolves to mps > cuda > cpu
    # Half precision (fp16) roughly doubles throughput on MPS and cuts
    # activation memory in half — with no visible impact on card art
    # (the compression down to 1490×2080 hides any residual noise).
    half_precision: bool = True
    _model: object | None = None
    _torch: object | None = None
    _resolved_device: str = ""
    _model_dtype: object | None = None

    def __post_init__(self) -> None:
        self.weights_path = Path(self.weights_path)
        if not self.weights_path.exists():
            raise UpscalerNotAvailable(
                f"MPS weights not found at {self.weights_path}. "
                "Run `python cli.py setup-upscaler --backend mps`."
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
        model = RRDBNet(num_in_ch=3, num_out_ch=3, num_feat=64,
                        num_block=23, num_grow_ch=32, scale=4)
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

    # Tile size for chunked inference on MPS. 256 with 16 px overlap runs
    # reliably on M-series GPUs (larger tiles occasionally stall the MPS
    # command buffer on macOS 26 for reasons we don't fully understand).
    # The overlap hides tile-boundary artefacts in the stitched output.
    tile_size: int = 256
    tile_pad: int = 16

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
        """Split the input into overlapping tiles, run each, stitch on CPU.

        The RRDBNet native scale is 4×. Each padded tile runs on device,
        the tile-proper region is cropped out (dropping the 4P overlap on
        each side that exists to hide seams), and copied back to a CPU-side
        accumulator. Keeping the accumulator on CPU means only one tile's
        worth of activation memory ever lives on the GPU at a time.
        """
        torch = self._torch
        device = self._resolved_device
        _, c, h, w = img.shape
        tile = self.tile_size
        pad = self.tile_pad
        scale = NATIVE_MODEL_SCALE

        # Preallocated CPU-side accumulator — no host↔device sync per tile
        # to update it (the sync happens only on the copy itself).
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

                # Cast back to float32 on the CPU accumulator so numpy sees
                # the full-precision output when we finally convert.
                out[:, :, y0*scale:y1*scale, x0*scale:x1*scale] = (
                    sr[:, :, top:bottom, left:right].to("cpu", dtype=self._torch.float32)
                )
        return out


def mps_weights_installed(vendor_dir: Path = MPS_VENDOR_DIR) -> bool:
    return (Path(vendor_dir) / MPS_WEIGHTS_FILENAME).exists()


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
                  cache_dir: Path = DEFAULT_CACHE_DIR,
                  upscaler: UpscaleBackend | None = None) -> Path:
    """Return the cached upscaled image path, creating it if missing.

    Idempotent: an existing non-empty output at the cache path is returned
    without re-running the backend. Callers can inject `upscaler` to swap
    the backend (used by tests and by any future MPS/PyTorch implementation).
    """
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    out = cache_dir / f"{scryfall_id}_face{face_index}_x{scale}.png"
    if out.exists() and out.stat().st_size > 0:
        return out

    if upscaler is None:
        upscaler = NcnnVulkanUpscaler()

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


def select_upscaler(backend: BackendName = "auto") -> UpscaleBackend:
    """Instantiate the best available upscaler.

    Priority when `backend == "auto"`:
      1. MPS (fastest on Apple Silicon — native, no Rosetta)
      2. ncnn-vulkan (works everywhere via the prebuilt binary)

    Explicit `mps` / `ncnn` forces that backend or raises if unavailable.
    """
    if backend == "mps":
        return MpsUpscaler()
    if backend == "ncnn":
        return NcnnVulkanUpscaler()
    if backend != "auto":
        raise ValueError(f"Unknown backend {backend!r} (expected auto|mps|ncnn)")

    # auto
    if torch_mps_available() and mps_weights_installed():
        try:
            up = MpsUpscaler()
            log.info("Using MPS upscaler backend (native Apple Silicon).")
            return up
        except UpscalerNotAvailable as e:
            log.warning("MPS backend unavailable, falling back: %s", e)
    if is_binary_installed():
        log.info("Using ncnn-vulkan upscaler backend.")
        return NcnnVulkanUpscaler()
    raise UpscalerNotAvailable(
        "No upscaler backend installed. Run `python cli.py setup-upscaler` "
        "(ncnn) or `python cli.py setup-upscaler --backend mps` (PyTorch/MPS)."
    )


# --- Small utilities -------------------------------------------------------
def is_binary_installed(vendor_dir: Path = DEFAULT_VENDOR_DIR) -> bool:
    """Cheap check without instantiating the upscaler."""
    return (Path(vendor_dir) / BINARY_NAME).exists()
