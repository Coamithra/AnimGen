"""Loader + helpers for model_library.json (the hand-authored model roster).

The JSON is the static source of truth for which models exist, their backend,
canonical cost/duration/resolution metadata, and notes. For Replicate models the
per-parameter input schema is fetched LIVE at config-edit time (see
backends/replicate_client.get_input_schema) so we don't duplicate every field here.
"""
from __future__ import annotations

import json
import random
import threading
from typing import Optional

from paths import MODEL_LIBRARY_PATH

# Guards writes to model_library.json (the Model Library refresh syncs capability flags
# off a daemon thread). Reads stay lock-free: atomic writes mean a reader sees either the
# whole old file or the whole new one (mirrors store.schema_cache's discipline).
_LIB_LOCK = threading.RLock()

# Sentinel stored in a shot's settings["seed"] meaning "pick a fresh random seed for
# every generation" (so each take - and every member of a future batch - differs). The
# concrete seed is resolved per-take at launch via resolve_seed() and recorded on the
# take, keeping a good result reproducible.
SEED_RANDOM = -1


def resolve_seed(seed):
    """The concrete seed to hand a backend for one take: a fresh random draw when the
    shot is set to SEED_RANDOM, otherwise the fixed seed unchanged (None passes through
    so the model picks its own)."""
    if seed == SEED_RANDOM:
        return random.randint(0, 2**31 - 1)
    return seed


def seed_label(seed) -> str:
    """Human-readable seed for summaries/gates: 'random' for the sentinel, else the value."""
    return "random" if seed == SEED_RANDOM else str(seed)


def load_library() -> dict:
    with open(MODEL_LIBRARY_PATH, encoding="utf-8") as f:
        return json.load(f)


def models() -> list[dict]:
    return load_library().get("models", [])


def get_model(model_id: str) -> Optional[dict]:
    for m in models():
        if m.get("id") == model_id:
            return m
    return None


def default_negative_prompt() -> str:
    return load_library().get("default_negative_prompt", "")


def _apply_capabilities(doc: dict, model_id: str, caps: dict) -> dict:
    """Pure: merge capability flags into the matching model in `doc` (mutated in place).
    Returns {field: (old, new)} for every flag that actually changed."""
    changed: dict = {}
    for m in doc.get("models", []):
        if m.get("id") == model_id:
            for k, v in caps.items():
                if m.get(k) != v:
                    changed[k] = (m.get(k), v)
                    m[k] = v
            break
    return changed


def sync_model_capabilities(model_id: str, caps: dict) -> dict:
    """Write derived capability flags back into model_library.json (the Model Library
    'Refresh from Replicate' action). Atomic + lock-guarded; only rewrites the file when a
    flag actually changed. Returns the {field: (old, new)} change diff."""
    from store.project import _atomic_write_json   # lazy: library is a low-level import
    with _LIB_LOCK:
        doc = load_library()
        changed = _apply_capabilities(doc, model_id, caps)
        if changed:
            _atomic_write_json(MODEL_LIBRARY_PATH, doc)
    return changed


def aspect_ratios(model_id: str) -> list[str]:
    """Allowed canvas aspect ratios for a model (authored per-model). Empty -> ['1:1']."""
    m = get_model(model_id)
    return (m or {}).get("aspect_ratios") or ["1:1"]


def estimate_cost(model_id: str, settings: dict) -> Optional[float]:
    """Best-effort USD estimate from cost_per_second_usd x duration (for the gate).

    Returns None when the model declares no rate (e.g. local $0 still returns 0.0).
    """
    m = get_model(model_id)
    if m is None:
        return None
    rate = m.get("cost_per_second_usd")
    if isinstance(rate, dict):                       # rate table keyed by a setting (see cost_by)
        rate = _keyed_rate(rate, settings, m)
    if rate is None:
        return None
    duration = settings.get("duration") or (m.get("default_params") or {}).get("duration") or 0
    try:
        return round(float(rate) * float(duration), 4)
    except (TypeError, ValueError):
        return float(rate) if rate == 0 else None


def _keyed_rate(rates: dict, settings: dict, m: dict) -> Optional[float]:
    """Pick a per-second rate from a rate table keyed by one setting. The setting is
    `cost_by` (default 'resolution', but e.g. 'mode' for Kling); fall back to the model's
    default for that setting, then to any listed rate."""
    axis = m.get("cost_by", "resolution")
    default = (m.get("default_params") or {}).get(axis)
    key = settings.get(axis)
    if key is None:
        key = default
    if key is not None and str(key) in rates:
        return rates[str(key)]
    if default is not None and str(default) in rates:
        return rates[str(default)]
    return next(iter(rates.values()), None)
