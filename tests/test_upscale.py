"""Tests for `proxy_studio.upscale`.

The real ncnn-vulkan binary is not exercised — it requires Vulkan/Metal GPU
access and pulls in a subprocess boundary that's out of scope for a unit
test. We inject a fake backend to prove the cache + DPI gate wiring is
correct, then trust the real backend's own self-check at `setup-upscaler`.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from proxy_studio import upscale as UP


# --- Fixtures --------------------------------------------------------------
@pytest.fixture
def scryfall_sized_png(tmp_path: Path) -> Path:
    """A 745×1040 PNG — Scryfall's native card PNG size."""
    p = tmp_path / "src.png"
    Image.new("RGB", (745, 1040), color=(120, 60, 60)).save(p)
    return p


class FakeUpscaler:
    """Doubles the pixel dimensions of the source. Deterministic + fast."""

    def __init__(self):
        self.calls: list[tuple[Path, Path]] = []

    def upscale(self, src: Path, out: Path, *, scale: int = 2) -> Path:
        self.calls.append((src, out))
        with Image.open(src) as im:
            resized = im.resize((im.width * scale, im.height * scale), Image.NEAREST)
            # `out` is a .png.tmp path — pass format explicitly so PIL doesn't
            # infer from the extension.
            resized.save(out, format="PNG")
        return out


# --- DPI gate --------------------------------------------------------------
class TestDpiGate:
    def test_scryfall_native_is_below_warn(self, scryfall_sized_png, caplog):
        # ~300 DPI < 550 warn threshold → log warning but do not raise.
        with caplog.at_level("WARNING"):
            UP.check_dpi_gate(scryfall_sized_png)
        assert any("below warn threshold" in r.message for r in caplog.records)

    def test_upscaled_2x_is_above_warn(self, tmp_path):
        # 1490 × 2080 = 2× Scryfall size → ~600 DPI, no warning.
        p = tmp_path / "big.png"
        Image.new("RGB", (1490, 2080), color=(0, 0, 0)).save(p)
        UP.check_dpi_gate(p)   # should not raise

    def test_below_fail_raises(self, tmp_path):
        # 500 × 700 → about 200 DPI, below default 290 hard-fail.
        p = tmp_path / "tiny.png"
        Image.new("RGB", (500, 700), color=(0, 0, 0)).save(p)
        with pytest.raises(ValueError, match="below hard-fail"):
            UP.check_dpi_gate(p)

    def test_effective_dpi_matches_layout_helper(self, scryfall_sized_png):
        # Sanity that upscale.effective_dpi_of agrees with layout.effective_dpi.
        from proxy_studio.layout import effective_dpi
        got = UP.effective_dpi_of(scryfall_sized_png)
        want = effective_dpi(745, 1040)
        assert pytest.approx(got, rel=1e-6) == want


# --- upscale_image cache behaviour ----------------------------------------
class TestUpscaleImageCache:
    def test_writes_output_with_expected_key(self, scryfall_sized_png, tmp_path):
        cache = tmp_path / "cache"
        fake = FakeUpscaler()
        out = UP.upscale_image(scryfall_sized_png, "abc", 0,
                                scale=2, cache_dir=cache, upscaler=fake)
        assert out == cache / "abc_face0_x2.png"
        assert out.exists() and out.stat().st_size > 0
        assert len(fake.calls) == 1

    def test_idempotent_second_call_skips_backend(self, scryfall_sized_png, tmp_path):
        cache = tmp_path / "cache"
        fake = FakeUpscaler()
        first = UP.upscale_image(scryfall_sized_png, "abc", 0,
                                  scale=2, cache_dir=cache, upscaler=fake)
        second = UP.upscale_image(scryfall_sized_png, "abc", 0,
                                    scale=2, cache_dir=cache, upscaler=fake)
        assert first == second
        assert len(fake.calls) == 1   # backend must not have been called again

    def test_different_face_produces_different_key(self, scryfall_sized_png, tmp_path):
        cache = tmp_path / "cache"
        fake = FakeUpscaler()
        a = UP.upscale_image(scryfall_sized_png, "abc", 0, cache_dir=cache, upscaler=fake)
        b = UP.upscale_image(scryfall_sized_png, "abc", 1, cache_dir=cache, upscaler=fake)
        assert a != b
        assert len(fake.calls) == 2

    def test_different_scale_produces_different_key(self, scryfall_sized_png, tmp_path):
        cache = tmp_path / "cache"
        fake = FakeUpscaler()
        a = UP.upscale_image(scryfall_sized_png, "abc", 0, scale=2,
                              cache_dir=cache, upscaler=fake)
        b = UP.upscale_image(scryfall_sized_png, "abc", 0, scale=4,
                              cache_dir=cache, upscaler=fake)
        assert a != b


# --- Backend availability --------------------------------------------------
class TestBackendAvailability:
    def test_missing_binary_raises(self, tmp_path):
        empty = tmp_path / "vendor-that-doesnt-exist"
        with pytest.raises(UP.UpscalerNotAvailable):
            UP.NcnnVulkanUpscaler(vendor_dir=empty)

    def test_is_binary_installed_false_for_missing(self, tmp_path):
        assert not UP.is_binary_installed(vendor_dir=tmp_path / "nowhere")

    def test_mps_weights_missing_raises(self, tmp_path):
        empty = tmp_path / "no-weights.pth"
        with pytest.raises(UP.UpscalerNotAvailable):
            UP.MpsUpscaler(weights_path=empty)

    def test_mps_weights_installed_false_for_missing(self, tmp_path):
        assert not UP.mps_weights_installed(vendor_dir=tmp_path / "nowhere")


class TestNeedsUpscale:
    def test_scryfall_native_needs_upscale(self, scryfall_sized_png):
        # 745×1040 → ~300 DPI at 63×88 mm → below 550 threshold, upscale it.
        assert UP.needs_upscale(scryfall_sized_png) is True

    def test_already_600_dpi_does_not_need_upscale(self, tmp_path):
        p = tmp_path / "big.png"
        Image.new("RGB", (1490, 2080), color=(0, 0, 0)).save(p)
        assert UP.needs_upscale(p) is False

    def test_way_bigger_than_needed_does_not_need_upscale(self, tmp_path):
        p = tmp_path / "huge.png"
        Image.new("RGB", (2980, 4160), color=(0, 0, 0)).save(p)
        assert UP.needs_upscale(p) is False

    def test_threshold_is_configurable(self, tmp_path):
        p = tmp_path / "mid.png"
        Image.new("RGB", (1200, 1680), color=(0, 0, 0)).save(p)
        # ~483 DPI — passes at 400 threshold, fails at 550.
        assert UP.needs_upscale(p, min_dpi=550.0) is True
        assert UP.needs_upscale(p, min_dpi=400.0) is False


class TestSelectUpscaler:
    def test_explicit_unknown_backend_raises(self):
        with pytest.raises(ValueError, match="Unknown backend"):
            UP.select_upscaler("nonexistent")

    def test_auto_falls_back_to_ncnn_when_mps_unavailable(self, tmp_path, monkeypatch):
        # No MPS weights, no torch-mps → should try ncnn. But no binary
        # either → should raise UpscalerNotAvailable.
        monkeypatch.setattr(UP, "torch_mps_available", lambda: False)
        monkeypatch.setattr(UP, "mps_weights_installed", lambda **_: False)
        monkeypatch.setattr(UP, "is_binary_installed", lambda **_: False)
        with pytest.raises(UP.UpscalerNotAvailable):
            UP.select_upscaler("auto")
