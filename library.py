"""Loader + helpers for model_library.json (the hand-authored model roster).

The JSON is the static source of truth for which models exist, their backend,
canonical cost/duration/resolution metadata, and notes. For Replicate models the
per-parameter input schema is fetched LIVE at config-edit time (see
backends/replicate_client.get_input_schema) so we don't duplicate every field here.
"""
from __future__ import annotations

import json
from typing import Optional

from paths import MODEL_LIBRARY_PATH


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


def estimate_cost(model_id: str, settings: dict) -> Optional[float]:
    """Best-effort USD estimate from cost_per_second_usd x duration (for the gate).

    Returns None when the model declares no rate (e.g. local $0 still returns 0.0).
    """
    m = get_model(model_id)
    if m is None:
        return None
    rate = m.get("cost_per_second_usd")
    if rate is None:
        return None
    duration = settings.get("duration") or (m.get("default_params") or {}).get("duration") or 0
    try:
        return round(float(rate) * float(duration), 4)
    except (TypeError, ValueError):
        return float(rate) if rate == 0 else None
