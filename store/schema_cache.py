"""On-disk cache of Replicate input schemas, keyed by replicate_model_id.

model_library.json is authored and deliberately omits per-parameter input schemas; they
are fetched LIVE from Replicate (backends.replicate_client.get_input_schema). This module
persists those fetched schemas to data/schema_cache.json so the **Model Library** tab can
fetch every model's schema once ("Fetch live schemas") and shot editors reuse the cached
schema instead of each tab re-fetching it.

File shape: {"format": "animgen-schema-cache", "version": 1,
             "schemas": {<replicate_model_id>: {"props": {...}, "fields": <int>,
                                                 "fetched": <unix-seconds>}}}

Read-only accessors tolerate a missing/unreadable/corrupt file (return nothing); `put`
tolerates only ABSENCE - a present-but-unreadable file makes it raise
`store._doc_io.UnreadableStoreError` instead of clobbering the other cached schemas (M11;
see `_load_doc`). Writes go through the project's atomic-write helper and are serialized
under a lock, so the off-thread fetch-all loop and the GUI thread reading entries can't
clobber each other (mirrors store.project's RLock + atomic JSON discipline). Paths are
read from `paths` at call time so tests can override `paths.SCHEMA_CACHE`, the same way
they override `paths.SCRATCH_DIR`.
"""
from __future__ import annotations

import threading
import time
from typing import Optional

import paths
from store._doc_io import UnreadableStoreError, read_doc
from store.project import _atomic_write_json

_FORMAT = "animgen-schema-cache"
_VERSION = 1
_lock = threading.RLock()


def _load_doc(*, strict: bool = False) -> dict:
    """The schemas dict on disk, or `{}` when the file is absent/empty.

    `strict=True` (used by put) lets an `UnreadableStoreError` propagate: a
    present-but-unreadable file (a transient Windows AV/indexer PermissionError, or corrupt
    JSON) must NOT be read as empty, or put would persist just this one schema and silently
    discard every other cached entry (M11 - same pattern, lower stakes since schemas re-fetch).
    `strict=False` (the read-only accessors) tolerates it and falls back to `{}`.
    """
    try:
        doc = read_doc(paths.SCHEMA_CACHE)
    except UnreadableStoreError:
        if strict:
            raise
        return {}
    if doc is None:
        return {}
    schemas = doc.get("schemas")
    return schemas if isinstance(schemas, dict) else {}


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
    """Store/overwrite a model's fetched schema and persist immediately. Returns the entry.

    Refuses to clobber a present-but-unreadable cache file (raises `UnreadableStoreError` via
    the strict load) rather than drop every other cached schema.
    """
    rec = {"props": props, "fields": len(props),
           "fetched": fetched if fetched is not None else time.time()}
    with _lock:
        schemas = _load_doc(strict=True)
        schemas[replicate_model_id] = rec
        _atomic_write_json(paths.SCHEMA_CACHE,
                           {"format": _FORMAT, "version": _VERSION, "schemas": schemas})
    return rec
