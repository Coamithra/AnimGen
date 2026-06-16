"""App-global preferences, persisted to data/app_settings.json.

A tiny key/value store for cross-project user preferences that don't belong to any one
`.animproj` document and shouldn't ride in app_state.json (which MainWindow rewrites
wholesale on every save via `_remember_last`). First key: `update_schemas_on_startup`
(whether to refresh every Replicate model's input schema at launch).

Same discipline as store.schema_cache / store.prompt_library: lock-guarded, atomic writes
through store.project._atomic_write_json, tolerant of a missing/corrupt file. Paths are
read from `paths` at call time so tests can override `paths.APP_SETTINGS`.
"""
from __future__ import annotations

import json
import threading

import paths
from store.project import _atomic_write_json

_FORMAT = "animgen-app-settings"
_VERSION = 1
_lock = threading.RLock()

# Known preference keys + their defaults (the source of truth for what exists).
UPDATE_SCHEMAS_ON_STARTUP = "update_schemas_on_startup"
_DEFAULTS = {UPDATE_SCHEMAS_ON_STARTUP: False}


def _load_doc() -> dict:
    try:
        with open(paths.APP_SETTINGS, encoding="utf-8") as f:
            doc = json.load(f)
    except (FileNotFoundError, OSError, ValueError):
        return {}
    return doc.get("settings", {}) if isinstance(doc, dict) else {}


def get_bool(key: str, default: bool | None = None) -> bool:
    """Read a boolean preference; falls back to the registered default (or `default`)."""
    fallback = _DEFAULTS.get(key, False) if default is None else default
    with _lock:
        val = _load_doc().get(key, fallback)
    return bool(val)


def set_bool(key: str, value: bool) -> None:
    """Store a boolean preference and persist immediately."""
    with _lock:
        settings = _load_doc()
        settings[key] = bool(value)
        _atomic_write_json(paths.APP_SETTINGS,
                           {"format": _FORMAT, "version": _VERSION, "settings": settings})
