"""Soft-delete (bin) and restore for results.

A delete moves the result's own files into data/bin/<result_id>/ and flags the row
deleted. CRITICAL: only files the tool OWNS (under data/) are moved - a result that
points at an existing project asset (e.g. a seeded shipped-move take in out/) is
flagged deleted but its external file is left exactly where it is. The tool is
purely additive and must never relocate the project's experiment assets.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from paths import BIN_DIR, DATA_DIR, RESULTS_DIR
from store.db import Store

_FILE_FIELDS = ("video_path", "preview_gif", "thumbnail")


def _tool_owned(p: Path) -> bool:
    try:
        return DATA_DIR.resolve() in p.resolve().parents
    except OSError:
        return False


def move_to_bin(result, store: Store) -> None:
    dest_dir = BIN_DIR / result.id
    updates: dict = {"deleted": True}
    for field in _FILE_FIELDS:
        val = getattr(result, field)
        if not val:
            continue
        p = Path(val)
        if p.exists() and _tool_owned(p):       # never move external project assets
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / p.name
            shutil.move(str(p), str(dest))
            updates[field] = str(dest)
    store.update_result(result.id, **updates)


def restore_from_bin(result, store: Store) -> None:
    dest_dir = RESULTS_DIR / result.config_id
    updates: dict = {"deleted": False}
    for field in _FILE_FIELDS:
        val = getattr(result, field)
        if not val:
            continue
        p = Path(val)
        if p.exists() and BIN_DIR.resolve() in p.resolve().parents:
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / p.name
            shutil.move(str(p), str(dest))
            updates[field] = str(dest)
    store.update_result(result.id, **updates)
