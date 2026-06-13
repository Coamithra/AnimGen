"""Frame extraction + thumbnails via PyAV (reuses the approach in review_clip.py).

first_frame/make_thumbnail power the results-grid icons; extract_frames is the
export path (Phase 5). All decode with `av`, installed in the animgen venv.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterator, Optional

import av
from PIL import Image


def iter_frames(video_path: str | Path) -> Iterator[Image.Image]:
    container = av.open(str(video_path))
    try:
        for frame in container.decode(video=0):
            yield frame.to_image()
    finally:
        container.close()


def first_frame(video_path: str | Path) -> Optional[Image.Image]:
    for im in iter_frames(video_path):
        return im
    return None


def video_info(video_path: str | Path) -> dict:
    container = av.open(str(video_path))
    try:
        s = container.streams.video[0]
        fps = float(s.average_rate) if s.average_rate else None
        return {"fps": fps, "frames": s.frames or None}
    finally:
        container.close()


def extract_frames(video_path: str | Path, out_dir: str | Path, prefix: str = "frame_") -> list[Path]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for i, im in enumerate(iter_frames(video_path)):
        p = out_dir / f"{prefix}{i:03d}.png"
        im.save(p)
        paths.append(p)
    return paths


def make_thumbnail(video_path: str | Path, out_path: str | Path, size: int = 256) -> Optional[Path]:
    im = first_frame(video_path)
    if im is None:
        return None
    im = im.convert("RGB")
    im.thumbnail((size, size))
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    im.save(out_path)
    return out_path
