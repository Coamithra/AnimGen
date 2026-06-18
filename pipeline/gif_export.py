"""Encode a take's video to an animated GIF (pure / Qt-free, headless-testable).

The take player offers right-click "Save as GIF…" / "Copy GIF to clipboard"; both go
through here. Decoding reuses pipeline.extract (PyAV -> PIL Images) and Pillow writes the
animated GIF natively, so there's no new dependency and the encode stays off the GUI thread.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from PIL import Image

from pipeline import extract

_DEFAULT_FPS = 12.0
_MAX_FRAMES = 1200      # mirrors ui.take_player._MAX_FRAMES safety cap


def encode_gif(frames: list[Image.Image], out_path: str | Path, fps: float, *,
               loop: int = 0, max_side: Optional[int] = None) -> Path:
    """Write `frames` (PIL Images) as an animated GIF at `out_path`; returns the path.

    `duration` is the per-frame hold in ms derived from fps (the GIF format stores it in
    centiseconds, so it rounds to ~10ms steps). `loop=0` is infinite. Each frame is a full,
    opaque, un-optimized RGB repaint (its own local palette), so it fully overwrites the
    previous one — `disposal=1` (leave-in-place) is the right choice and there's nothing to
    ghost. `max_side` optionally downscales the longest edge."""
    if not frames:
        raise ValueError("no frames to encode")
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fps = fps if fps and fps > 0 else _DEFAULT_FPS
    duration = max(1, round(1000.0 / fps))

    prepared = []
    for im in frames:
        im = im.convert("RGB")
        if max_side and max(im.size) > max_side:
            im = im.copy()
            im.thumbnail((max_side, max_side))
        prepared.append(im)

    first, rest = prepared[0], prepared[1:]
    first.save(out_path, format="GIF", save_all=True, append_images=rest,
               duration=duration, loop=loop, disposal=1)
    return out_path


def take_to_gif(source: str | Path, out_path: str | Path, *, fps: Optional[float] = None,
                max_side: Optional[int] = None, max_frames: int = _MAX_FRAMES) -> Path:
    """Decode a take's video file and write it as an animated GIF.

    fps falls back to the clip's measured rate, then _DEFAULT_FPS. Decoding is capped at
    `max_frames` so a pathologically long clip can't exhaust memory (mirrors the player)."""
    source = str(source)
    if fps is None:
        try:
            fps = extract.video_info(source).get("fps")
        except Exception:       # noqa: BLE001 - fall back to the default rate on any probe failure
            fps = None
    frames: list[Image.Image] = []
    for i, im in enumerate(extract.iter_frames(source)):
        if i >= max_frames:
            break
        frames.append(im)
    return encode_gif(frames, out_path, float(fps or _DEFAULT_FPS), max_side=max_side)
