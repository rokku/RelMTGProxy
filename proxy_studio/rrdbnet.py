"""RRDBNet architecture — the Real-ESRGAN generator.

Inlined so we can load the `RealESRGAN_x4plus.pth` weights without pulling
in `basicsr` (which has a long history of breaking on newer PyTorch and
torchvision releases).

Reference: Wang et al., ESRGAN (2018) + Real-ESRGAN (2021).
Original code: https://github.com/xinntao/Real-ESRGAN/blob/master/realesrgan/archs/srvgg_arch.py
"""

from __future__ import annotations

import torch
import torch.nn as nn
import torch.nn.functional as F


class _ResidualDenseBlock(nn.Module):
    """One RDB (residual dense block). Five convolutions with dense skips."""

    def __init__(self, num_feat: int = 64, num_grow_ch: int = 32) -> None:
        super().__init__()
        self.conv1 = nn.Conv2d(num_feat, num_grow_ch, 3, 1, 1)
        self.conv2 = nn.Conv2d(num_feat + num_grow_ch, num_grow_ch, 3, 1, 1)
        self.conv3 = nn.Conv2d(num_feat + 2 * num_grow_ch, num_grow_ch, 3, 1, 1)
        self.conv4 = nn.Conv2d(num_feat + 3 * num_grow_ch, num_grow_ch, 3, 1, 1)
        self.conv5 = nn.Conv2d(num_feat + 4 * num_grow_ch, num_feat, 3, 1, 1)
        self.lrelu = nn.LeakyReLU(negative_slope=0.2, inplace=True)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x1 = self.lrelu(self.conv1(x))
        x2 = self.lrelu(self.conv2(torch.cat((x, x1), 1)))
        x3 = self.lrelu(self.conv3(torch.cat((x, x1, x2), 1)))
        x4 = self.lrelu(self.conv4(torch.cat((x, x1, x2, x3), 1)))
        x5 = self.conv5(torch.cat((x, x1, x2, x3, x4), 1))
        # Constant residual scaling per the ESRGAN paper.
        return x5 * 0.2 + x


class _RRDB(nn.Module):
    """Residual-in-residual dense block. Three RDBs with an outer residual."""

    def __init__(self, num_feat: int, num_grow_ch: int = 32) -> None:
        super().__init__()
        self.rdb1 = _ResidualDenseBlock(num_feat, num_grow_ch)
        self.rdb2 = _ResidualDenseBlock(num_feat, num_grow_ch)
        self.rdb3 = _ResidualDenseBlock(num_feat, num_grow_ch)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        out = self.rdb1(x)
        out = self.rdb2(out)
        out = self.rdb3(out)
        return out * 0.2 + x


class RRDBNet(nn.Module):
    """The Real-ESRGAN 4× generator.

    Defaults match `RealESRGAN_x4plus.pth` (64 features, 23 blocks, 4× scale).
    """

    def __init__(self, num_in_ch: int = 3, num_out_ch: int = 3,
                 num_feat: int = 64, num_block: int = 23,
                 num_grow_ch: int = 32, scale: int = 4) -> None:
        super().__init__()
        if scale != 4:
            # For 2× we'd need pixel-shuffle; the x4plus weights are 4× only.
            raise ValueError("RRDBNet in this project only supports scale=4")
        self.scale = scale
        self.conv_first = nn.Conv2d(num_in_ch, num_feat, 3, 1, 1)
        self.body = nn.Sequential(*[
            _RRDB(num_feat, num_grow_ch) for _ in range(num_block)
        ])
        self.conv_body = nn.Conv2d(num_feat, num_feat, 3, 1, 1)
        # Upsampling: two nearest-neighbour × 2 stages, each followed by a conv.
        self.conv_up1 = nn.Conv2d(num_feat, num_feat, 3, 1, 1)
        self.conv_up2 = nn.Conv2d(num_feat, num_feat, 3, 1, 1)
        self.conv_hr = nn.Conv2d(num_feat, num_feat, 3, 1, 1)
        self.conv_last = nn.Conv2d(num_feat, num_out_ch, 3, 1, 1)
        self.lrelu = nn.LeakyReLU(negative_slope=0.2, inplace=True)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        feat = self.conv_first(x)
        body_feat = self.conv_body(self.body(feat))
        feat = feat + body_feat
        feat = self.lrelu(self.conv_up1(
            F.interpolate(feat, scale_factor=2, mode="nearest")))
        feat = self.lrelu(self.conv_up2(
            F.interpolate(feat, scale_factor=2, mode="nearest")))
        return self.conv_last(self.lrelu(self.conv_hr(feat)))
