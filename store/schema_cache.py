"""On-disk cache of Replicate input schemas, keyed by replicate_model_id.

model_library.json is authored and deliberately omits per-parameter input schemas; they
are fetched LIVE from Replicate (backends.replicate_client.get_input_schema). This module
persists those fetched schemas to data/schema_cache.json so the **Model Library** tab can
fetch every model's schema once ("Fetch live schemas") and shot editors reuse the cached
schema instead of each tab re-fetching it.

File shape: {"format": "animgen-schema-cache", "version": 1,
             "schemas": {<replicate_model_id>: {"props": {...}, "fields": <int>,
                                                 "fetched": <unix-seconds>}}}

Reads tolerate a missing/corrupt file (returns nothing). Writes go through the project's
atomic-write helper and are serialized under a lock, so the off-thread fetch-all loop and
the GUI thread reading entries can't clobber each other (mirrors store.project's RLock +
atomic JSON discipline). Paths are read from `paths` at call time so tests can override
`paths.SCHEMA_CACHE`, the same way they override `paths.SCRATCH_DIR`.
"""
from __future__ import annotations

import json
import threading
import time
from typing import Optional

import paths
from store.project import _atomic_write_json

_FORMAT = "animgen-schema-cache"
_VERSION = 1
_lock = threading.RLock()


def _load_doc() -> dict:
    try:
        with open(paths.SCHEMA_CACHE, encoding="utf-8") as f:
            doc = json.load(f)
    except (FileNotFoundError, OSError, ValueError):
        return {}
    return doc.get("schemas", {}) if isinstance(doc, dict) else {}


def all_entries() -> dict:
    """Every cached entry, keyed by replicate_model_id (a copy; safe to read freely)."""
    with _lock:
        return dict(_load_doc())


def entry(replicate_model_id: Optional[str]) -> Optional[dict]:
    """The full cache entry ({props, fields, fetched}) for a model, or None if uncached."""
    if not replicate_model_id:
        return None
    with _lock:
        return _load_doc().get(replicate_model_id)


def get(replicate_model_id: Optional[str]) -> Optional[dict]:
    """Just the cached input-schema `props` for a model (what the shot editor wants), or None."""
    e = entry(replicate_model_id)
    return e.get("props") if e else None


def put(replicate_model_id: str, props: dict, *, fetched: Optional[float] = None) -> dict:
    """Store/overwrite a model's fetched schema and persist immediately. Returns the entry."""
    rec = {"props": props, "fields": len(props),
           "fetched": fetched if fetched is not None else time.time()}
    with _lock:
        schemas = _load_doc()
        schemas[replicate_model_id] = rec
        _atomic_write_json(paths.SCHEMA_CACHE,
                           {"format": _FORMAT, "version": _VERSION, "schemas": schemas})
    return rec
