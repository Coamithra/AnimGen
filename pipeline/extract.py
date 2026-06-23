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


def probe_media_fields(video_path: str | Path | None,
                       fps: Optional[float] = None,
                       frame_count: Optional[int] = None) -> tuple[Optional[float], Optional[int]]:
    """Return (fps, frame_count), filling any that is None by probing video_path.

    Backend runners report neither fps nor frame_count, so a finished take would carry
    None for both (settings.txt then prints 'fps: None'). This stamps them off the produced
    video. Best-effort: a missing/unreadable file leaves the unfilled field None and never
    raises, so it can't fail a take that actually rendered."""
    if not video_path or (fps is not None and frame_count is not None):
        return fps, frame_count
    try:
        info = video_info(video_path)
    except Exception:  # noqa: BLE001 - probing is best-effort; a bad file leaves fields None
        return fps, frame_count
    if fps is None:
        fps = info.get("fps")
    if frame_count is None:
        frame_count = info.get("frames")
    return fps, frame_count


def extract_frames(video_path: str | Path, out_dir: str | Path, prefix: str = "frame_") -> list[Path]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    # Zero-pad the frame index to a fixed width so a lexicographic sort (sprite-sheet
    # assembly, frame-set reimport) always matches true frame order: a flat {i:03d} breaks
    # once the count passes 999 (frame_1000 sorts before frame_999). Size the pad from the
    # true decoded count via a counting pass rather than the container's declared
    # stream.frames, which is often 0/None or an estimate (VFR, gif, streamed) — a wrong
    # nonzero count would set the width too narrow and silently re-break the sort. The
    # floor of 3 keeps the historic frame_000 look for short takes.
    total = sum(1 for _ in iter_frames(video_path))
    width = max(3, len(str(total - 1))) if total else 3
    paths = []
    for i, im in enumerate(iter_frames(video_path)):
        p = out_dir / f"{prefix}{i:0{width}d}.png"
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
