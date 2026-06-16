"""Soft-delete (bin) and restore for takes.

A delete moves the take's own files into the project's <assets>/.bin/<take_id>/ and
flags the take deleted. CRITICAL: only files the project OWNS (under its assets dir)
are moved - a take that points at an existing external asset (e.g. a seeded Fighter
take in ../Fighter/out) is flagged deleted but its external file is left exactly where
it is. The tool is purely additive and must never relocate external assets (gotcha #2).
"""
from __future__ import annotations

import shutil
from pathlib import Path

_FILE_FIELDS = ("video_path", "preview_gif", "thumbnail")


def _under(p: Path, base: Path) -> bool:
    try:
        return base.resolve() in p.resolve().parents
    except OSError:
        return False


def move_to_bin(take, project) -> None:
    dest_dir = project.bin_dir / take.id
    updates: dict = {"deleted": True}
    for field in _FILE_FIELDS:
        val = getattr(take, field)
        if not val:
            continue
        p = Path(val)
        if p.exists() and _under(p, project.assets_dir):   # never move external assets
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / p.name
            shutil.move(str(p), str(dest))
            updates[field] = str(dest)
    project.update_take(take.id, **updates)


def restore_from_bin(take, project) -> None:
    updates: dict = {"deleted": False}
    for field in _FILE_FIELDS:
        val = getattr(take, field)
        if not val:
            continue
        p = Path(val)
        if p.exists() and _under(p, project.bin_dir):
            dest_dir = project.thumbs_dir if field == "thumbnail" else project.takes_dir
            dest = dest_dir / p.name
            shutil.move(str(p), str(dest))
            updates[field] = str(dest)
    project.update_take(take.id, **updates)
