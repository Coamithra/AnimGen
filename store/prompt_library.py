"""App-global library of reusable prompt prefabs (positive + negative pairs).

Unlike a shot's own prompt (which lives in the .animproj), these templates are shared
across every project so reusable choreography phrasing / camera-lock terms can be saved
once and applied anywhere. Persisted to data/prompt_templates.json.

File shape: {"format": "animgen-prompt-templates", "version": 1,
             "templates": [{"name": <str>, "positive": <str>, "negative": <str>}, ...]}

Reads tolerate a missing/corrupt file (returns the seed templates). Writes go through the
project's atomic-write helper under a lock, mirroring store.schema_cache's discipline.
Paths are read from `paths` at call time so tests can override `paths.PROMPT_TEMPLATES`.
"""
from __future__ import annotations

import json
import threading
from typing import Optional

import paths
import library
from store.project import _atomic_write_json

_FORMAT = "animgen-prompt-templates"
_VERSION = 1
_lock = threading.RLock()

# Shipped starter prefabs. The negative side reuses the authored default (Seedance etc.
# ignore it, but the models that accept a negative get the same baseline the editor uses).
_SEEDS = [
    {"name": "Camera-locked action",
     "positive": "fixed camera, character performs the action in place, clean loop, "
                 "no camera movement, consistent background",
     "negative": library.default_negative_prompt()},
    {"name": "Snappy impact",
     "positive": "sharp anticipation then explosive impact, strong weight transfer, "
                 "exaggerated keyframes, snappy timing, fixed camera",
     "negative": library.default_negative_prompt()},
]


def _normalize(t: dict) -> dict:
    return {"name": str(t.get("name", "")).strip(),
            "positive": str(t.get("positive", "")),
            "negative": str(t.get("negative", ""))}


def _load_list() -> list[dict]:
    try:
        with open(paths.PROMPT_TEMPLATES, encoding="utf-8") as f:
            doc = json.load(f)
    except (FileNotFoundError, OSError, ValueError):
        return [dict(t) for t in _SEEDS]
    items = doc.get("templates") if isinstance(doc, dict) else None
    if not isinstance(items, list):
        return [dict(t) for t in _SEEDS]
    return [_normalize(t) for t in items if isinstance(t, dict) and str(t.get("name", "")).strip()]


def _write(items: list[dict]) -> None:
    _atomic_write_json(paths.PROMPT_TEMPLATES,
                       {"format": _FORMAT, "version": _VERSION, "templates": items})


def all_templates() -> list[dict]:
    """Every template, name-sorted (a copy; safe to read freely)."""
    with _lock:
        return sorted(_load_list(), key=lambda t: t["name"].lower())


def get(name: str) -> Optional[dict]:
    """The template with this name (exact match), or None."""
    with _lock:
        for t in _load_list():
            if t["name"] == name:
                return t
    return None


def save(name: str, positive: str, negative: str) -> dict:
    """Add or overwrite (by name) a template and persist immediately. Returns the entry."""
    rec = _normalize({"name": name, "positive": positive, "negative": negative})
    if not rec["name"]:
        raise ValueError("Template name is required")
    with _lock:
        items = [t for t in _load_list() if t["name"] != rec["name"]]
        items.append(rec)
        _write(items)
    return rec


def delete(name: str) -> bool:
    """Remove a template by name. Returns True if one was removed."""
    with _lock:
        items = _load_list()
        kept = [t for t in items if t["name"] != name]
        if len(kept) == len(items):
            return False
        _write(kept)
    return True
