"""Keypose framing/normalization - the engine behind the crop tool.

Models the proven approach from scripts/normalize_keypose.py: use the top-left
corner as the background reference, keep foreground blobs above a size threshold
(preserves detached VFX like dizzy stars), then masked-paste onto a canonical
#FF00FF canvas. Adds a crop rect and scale-to-target-character-height so a raw
source (keypose export, harvested video frame, any sprite) becomes a
contract-normalized keypose: fixed canvas, ground line, magenta background.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import numpy as np
from PIL import Image
from scipy import ndimage

MAGENTA = (255, 0, 255)


def _foreground(arr: np.ndarray, bg: np.ndarray, bg_thresh: int, min_blob: int) -> np.ndarray:
    """Boolean foreground mask via a given bg reference + blob-size keep + dilate.

    `bg` is sampled from the FULL source corner (not the crop) so a tight crop that
    lands the character on its own corner still keys correctly."""
    a = arr.astype(int)
    m = np.abs(a - bg).sum(axis=2) > bg_thresh
    lab, _ = ndimage.label(m, np.ones((3, 3)))
    sizes = np.bincount(lab.ravel())
    sizes[0] = 0
    keep = np.isin(lab, np.where(sizes >= min_blob)[0])
    for _ in range(2):  # dilate so anti-aliased edges keep their blend
        keep[1:, :] |= keep[:-1, :]; keep[:-1, :] |= keep[1:, :]
        keep[:, 1:] |= keep[:, :-1]; keep[:, :-1] |= keep[:, 1:]
    return keep


def normalize_keypose(
    src: str | Path, *, crop: Optional[tuple[int, int, int, int]] = None,
    canvas: tuple[int, int] = (1254, 1254), char_height_frac: float = 0.65,
    ground_y: Optional[int] = None, char_x: float = 0.5, scale: Optional[float] = None,
    min_blob: int = 50, bg_thresh: int = 60, out_path: Optional[str | Path] = None,
) -> dict:
    """Crop, scale to a target character height, place on a magenta canvas.

    crop is (x, y, w, h) in source pixels (None = whole image). If `scale` is given
    it overrides char_height_frac. Returns metadata + the PIL image; writes a PNG
    when out_path is set.
    """
    im = Image.open(src).convert("RGB")
    bg_ref = np.array(im)[0, 0].astype(int)   # full-image corner = the contract bg
    if crop:
        x, y, w, h = (int(v) for v in crop)
        im = im.crop((x, y, x + w, y + h))
    arr = np.array(im)
    keep = _foreground(arr, bg_ref, bg_thresh, min_blob)
    ys, xs = np.where(keep)
    if len(ys) == 0:
        raise ValueError("no foreground found in crop (background-only region?)")

    cw, ch = canvas
    char_h = int(ys.max() - ys.min() + 1)
    s = float(scale) if scale is not None else (char_height_frac * ch) / char_h

    if s != 1.0:
        small = im.resize((max(1, round(im.size[0] * s)), max(1, round(im.size[1] * s))),
                          Image.Resampling.LANCZOS)
        smask = Image.fromarray((keep * 255).astype("uint8")).resize(
            small.size, Image.Resampling.LANCZOS)
    else:
        small = im
        smask = Image.fromarray((keep * 255).astype("uint8"))

    gy = int(ground_y) if ground_y is not None else round(ch * 0.94)
    cx_center = (xs.min() + xs.max()) / 2.0
    px = round(char_x * cw - cx_center * s)
    py = gy - round(ys.max() * s)

    out = Image.new("RGB", (cw, ch), MAGENTA)
    out.paste(small, (px, py), smask)

    # re-measure the placed character for provenance
    b = np.array(out).astype(int)
    placed = np.abs(b - np.array(MAGENTA)).sum(axis=2) > bg_thresh
    pys, pxs = np.where(placed)
    char_box = ([int(pxs.min()), int(pys.min()), int(pxs.max()), int(pys.max())]
                if len(pys) else None)

    if out_path is not None:
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        out.save(out_path)

    return {
        "image": out, "canvas": [cw, ch], "scale": round(s, 5), "ground_y": gy,
        "char_x": char_x, "char_box": char_box,
        "crop": list(crop) if crop else None,
    }
