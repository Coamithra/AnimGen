"""Chroma-key background replacement for keyframe assets (pure / Qt-free, headless-testable).

The Assets tab's "Replace background" runs this: key a flat chroma screen out of a source
sprite and composite the result onto a solid fill (default magenta, the generation contract).
The keyer is an **analytic unmix** ported + generalized from Fighter's `unmixMagenta`
(`src/render/assets.ts`): it models each pixel as `observed = a*fg + (1-a)*bg` and, from the
per-pixel spill along the screen's colour axis, recovers a soft alpha AND the despilled
foreground colour — feathered edges with no coloured halo, unlike `framing._foreground`'s
binary corner threshold.

Generalized by the canonical chroma's channel structure (hi = channels that are 255, lo =
channels that are 0):
  - 2 hi / 1 lo (magenta, cyan, yellow):  spill = min(hi1, hi2) - lo
  - 1 hi / 2 lo (red,  green, blue):      spill = hi - max(lo1, lo2)
then a = 255 - spill; subtract spill from the hi channels; scale every channel by 255/a;
multiply source alpha by a/255. Below `fg_max` a pixel is clean foreground (untouched); above
`bg_min` it snaps fully transparent.

A corner that isn't near any supported chroma (photographic / grey / white background) has no
valid unmix, so `key_to_transparent` falls back to the existing binary corner keyer
(`framing._foreground`) under the AUTO source.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import numpy as np
from PIL import Image

from pipeline import framing

# Canonical chroma screens, ordered by how common they are as a source screen. Values are the
# pure extremes the unmix math assumes; the UI keys/labels come straight from these names.
SUPPORTED_CHROMA: dict[str, tuple[int, int, int]] = {
    "Magenta": (255, 0, 255),
    "Green": (0, 255, 0),
    "Blue": (0, 0, 255),
    "Cyan": (0, 255, 255),
    "Yellow": (255, 255, 0),
    "Red": (255, 0, 0),
}

# The generation contract fill (see CLAUDE.md "why magenta"); the default target.
CONTRACT_FILL: tuple[int, int, int] = (255, 0, 255)

# Sentinel source: no chroma screen, key off the corner with the binary threshold instead.
AUTO = "Auto (corner threshold)"

# Match unmixMagenta's cutoffs: below FG_MAX a pixel is clean fg, above BG_MIN it's pure bg.
_FG_MAX = 8
_BG_MIN = 230
# _foreground params for the AUTO fallback (mirror framing.keyed_sprite's defaults).
_AUTO_BG_THRESH = 60
_AUTO_MIN_BLOB = 50


def sample_corner(img: Image.Image) -> tuple[int, int, int]:
    """The top-left corner pixel as an (r, g, b) tuple — the background reference."""
    px = np.array(img.convert("RGB"))[0, 0]
    return int(px[0]), int(px[1]), int(px[2])


def nearest_chroma(rgb: tuple[int, int, int], max_dist: float = 90.0) -> Optional[str]:
    """Snap an (r, g, b) to the nearest SUPPORTED_CHROMA name, or None if none is within
    `max_dist` (Euclidean in RGB) — the caller then prefills AUTO."""
    best: Optional[str] = None
    best_d: Optional[float] = None
    for name, c in SUPPORTED_CHROMA.items():
        d = ((rgb[0] - c[0]) ** 2 + (rgb[1] - c[1]) ** 2 + (rgb[2] - c[2]) ** 2) ** 0.5
        if best_d is None or d < best_d:
            best, best_d = name, d
    return best if best_d is not None and best_d <= max_dist else None


def unmix_chroma(px: np.ndarray, bg_rgb, fg_max: int = _FG_MAX, bg_min: int = _BG_MIN) -> np.ndarray:
    """Unmix a flat chroma `bg_rgb` out of an (H, W, 4) uint8 RGBA array, in place; returns it.

    `bg_rgb` must be a canonical chroma (channels 0 or 255). A colour that is all-low or
    all-high (black/white/grey) has no screen axis and is returned unchanged."""
    hi = [i for i in range(3) if bg_rgb[i] >= 128]
    lo = [i for i in range(3) if bg_rgb[i] < 128]
    if len(hi) not in (1, 2):
        return px

    f = px.astype(np.float32)
    chan = [f[..., 0], f[..., 1], f[..., 2]]
    if len(hi) == 2:
        spill = np.minimum(chan[hi[0]], chan[hi[1]]) - chan[lo[0]]
    else:
        spill = chan[hi[0]] - np.maximum(chan[lo[0]], chan[lo[1]])
    spill = np.clip(spill, 0.0, 255.0)

    alpha = 255.0 - spill
    scale = 255.0 / np.where(alpha > 0, alpha, 1.0)
    out = f.copy()
    for i in hi:
        out[..., i] = chan[i] - spill
    for i in range(3):
        out[..., i] = out[..., i] * scale
    out[..., 3] = f[..., 3] * alpha / 255.0

    mid = (spill > fg_max) & (spill < bg_min)
    full = spill >= bg_min
    keep = ~mid & ~full
    result = np.where(keep[..., None], f, out)          # clean fg stays exactly as-is
    result = np.where(full[..., None], 0.0, result)      # pure bg -> fully transparent
    px[...] = np.clip(np.round(result), 0, 255).astype(np.uint8)
    return px


def key_to_transparent(img: Image.Image, source: str) -> Image.Image:
    """Key `source`'s background out of `img`; return a transparent RGBA sprite.

    A named SUPPORTED_CHROMA runs the analytic unmix (soft alpha + despill); AUTO (or any
    unknown source) falls back to the binary corner keyer used at generation time."""
    arr = np.array(img.convert("RGBA"))
    if source in SUPPORTED_CHROMA:
        unmix_chroma(arr, SUPPORTED_CHROMA[source])
        return Image.fromarray(arr, "RGBA")
    rgb = arr[..., :3].astype(int)
    bg = rgb[0, 0]
    keep = framing._foreground(rgb, bg, _AUTO_BG_THRESH, _AUTO_MIN_BLOB)
    arr[..., 3] = np.where(keep, arr[..., 3], 0)
    return Image.fromarray(arr, "RGBA")


def composite_over(rgba_img: Image.Image, fill_rgb) -> Image.Image:
    """Flatten a transparent sprite onto a solid `fill_rgb`; return an opaque RGB image."""
    rgba = rgba_img.convert("RGBA")
    bg = Image.new("RGBA", rgba.size, (int(fill_rgb[0]), int(fill_rgb[1]), int(fill_rgb[2]), 255))
    return Image.alpha_composite(bg, rgba).convert("RGB")


def replace_background(img: Image.Image, source: str, fill_rgb) -> tuple[Image.Image, Image.Image]:
    """Key `source` out and composite onto `fill_rgb`. Returns (opaque_rgb, transparent_rgba)
    so the caller can persist the transparent sprite for a later lossless re-fill."""
    transparent = key_to_transparent(img, source)
    return composite_over(transparent, fill_rgb), transparent


def has_transparency(img: Image.Image) -> bool:
    """True if the image carries an alpha channel with any non-opaque pixel (or palette
    transparency) — the marker that forces a background composite on import."""
    if img.mode in ("RGBA", "LA") or (img.mode == "PA"):
        return img.getchannel("A").getextrema()[0] < 255
    if img.mode == "P":
        return "transparency" in img.info
    return False


def load_image(path: str | Path) -> Image.Image:
    """Open an image file (kept here so callers don't import PIL directly)."""
    return Image.open(path)
