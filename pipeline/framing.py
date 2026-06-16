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


def canvas_size(aspect: str, *, local: bool = False, budget: int = 409_600,
                long_side: int = 1254) -> tuple[int, int]:
    """(width, height) for an 'W:H' aspect string.

    Hosted: the longest side is `long_side` (the model resizes to its own resolution).
    Local (ComfyUI): fit to ~`budget` total pixels with both dims snapped to a multiple
    of 16 (Wan's latent grid), so any aspect costs ~the same VRAM as the 640^2 default.
    """
    try:
        wr, hr = (float(x) for x in aspect.split(":"))
        if wr <= 0 or hr <= 0:
            raise ValueError
    except (ValueError, AttributeError):
        wr = hr = 1.0
    if local:
        w = (budget * wr / hr) ** 0.5
        return max(16, round(w / 16) * 16), max(16, round((w * hr / wr) / 16) * 16)
    if wr >= hr:
        return long_side, max(2, round(long_side * hr / wr))
    return max(2, round(long_side * wr / hr)), long_side


def _short_side(resolution: Optional[str]) -> Optional[int]:
    """'720p' -> 720, '1080p' -> 1080. None / unparseable -> None."""
    if not resolution:
        return None
    digits = "".join(ch for ch in str(resolution) if ch.isdigit())
    return int(digits) if digits else None


def display_size(aspect: str, *, resolution: Optional[str] = None,
                 local: bool = False) -> tuple[int, int]:
    """(width, height) of a model's *effective output* - for the editor readout only.

    Local (ComfyUI): the budget canvas IS the render size -> canvas_size(local=True).
    Hosted: derive from the resolution tier ('480p'/'720p'/'1080p' = that many px on the
    SHORTER side) combined with the aspect, so e.g. 480p and 720p of the same model read
    differently. Unknown resolution -> fall back to the 1254 framing canvas.

    Display-only: generation still frames the keypose at canvas_size() (the deliberate
    1254 contract); placement is normalized, so this never changes what gets rendered.
    """
    if local:
        return canvas_size(aspect, local=True)
    short = _short_side(resolution)
    if short is None:
        return canvas_size(aspect, local=False)
    try:
        wr, hr = (float(x) for x in aspect.split(":"))
        if wr <= 0 or hr <= 0:
            raise ValueError
    except (ValueError, AttributeError):
        wr = hr = 1.0
    if wr >= hr:                                   # landscape / square: height is the short side
        return max(2, round(short * wr / hr)), short
    return short, max(2, round(short * hr / wr))   # portrait: width is the short side


def keyed_sprite(src: str | Path, *, bg_thresh: int = 60, min_blob: int = 50,
                 max_side: Optional[int] = None) -> Image.Image:
    """Key the foreground off the corner background; return it as an RGBA image cropped to
    the sprite's bounding box (alpha = the kept mask). No foreground found -> whole image.

    max_side downsamples the source's longest side before keying (cheap previews/thumbnails
    avoid keying at full contract resolution); None keys at native resolution."""
    im = Image.open(src).convert("RGB")
    if max_side and max(im.size) > max_side:
        im.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    arr = np.array(im)
    bg = arr[0, 0].astype(int)
    keep = _foreground(arr, bg, bg_thresh, min_blob)
    ys, xs = np.where(keep)
    if len(ys) == 0:
        return im.convert("RGBA")
    x0, x1, y0, y1 = int(xs.min()), int(xs.max()), int(ys.min()), int(ys.max())
    rgba = np.dstack([arr, (keep * 255).astype("uint8")])
    return Image.fromarray(rgba[y0:y1 + 1, x0:x1 + 1], "RGBA")


def render_placement(asset: str | Path, placement: dict, canvas: tuple[int, int],
                     out_path: Optional[str | Path] = None,
                     *, sprite: Optional[Image.Image] = None) -> Image.Image:
    """Paste a keyed sprite onto a magenta canvas per a normalized placement
    {scale: sprite-height / canvas-height, cx, cy: center as 0..1 of the canvas}.

    Pass `sprite` (a pre-keyed RGBA image) to skip the keying step - lets a caller
    cache the keyed sprite and re-render placements/canvases cheaply (e.g. thumbnails)."""
    W, H = int(canvas[0]), int(canvas[1])
    scale = float(placement.get("scale", 0.65))
    cx = float(placement.get("cx", 0.5))
    cy = float(placement.get("cy", 0.6))
    sprite = sprite if sprite is not None else keyed_sprite(asset)
    target_h = max(1, round(scale * H))
    ratio = target_h / sprite.height
    sprite = sprite.resize((max(1, round(sprite.width * ratio)), target_h),
                           Image.Resampling.LANCZOS)
    out = Image.new("RGB", (W, H), MAGENTA)
    out.paste(sprite, (round(cx * W - sprite.width / 2), round(cy * H - sprite.height / 2)), sprite)
    if out_path is not None:
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        out.save(out_path)
    return out


def render_keyposes(shot, out_dir: str | Path) -> tuple[Optional[str], Optional[str]]:
    """Frame a shot's start/end keyframes at generation time: paste each keyed sprite onto
    the shot's canvas at its own normalized placement (shot.crop['start'|'end']). Returns
    (start_png, end_png) under out_dir; either may be None."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    crop = shot.crop or {}
    canvas = (getattr(shot, "canvas_w", None) or 1254, getattr(shot, "canvas_h", None) or 1254)
    start_out = end_out = None
    if shot.start_frame and Path(shot.start_frame).exists():
        start_out = str(out_dir / "start.png")
        render_placement(shot.start_frame, crop.get("start") or {}, canvas, start_out)
    if shot.end_frame and Path(shot.end_frame).exists():
        end_out = str(out_dir / "end.png")
        render_placement(shot.end_frame, crop.get("end") or {}, canvas, end_out)
    return start_out, end_out
