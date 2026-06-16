"""Dataclasses mirroring the SQLite schema in store/db.py.

- Config: one "animation configuration" row - start/end keypose, framing, prompt,
  model + params. The user-facing rows in the main window.
- Result: one generated take. Its `settings_snapshot` is an IMMUTABLE copy of the
  exact config + resolved model params at the moment it was launched, so a result
  stays linked to what made it even if the config is later edited. This fills the
  project's current "no per-take metadata" gap.
- Job: one generation in the queue (backend + external id + state/log).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# Result status values (also the folder-view badge labels).
STATUS_PENDING = "pending"
STATUS_GENERATING = "generating"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"   # queued generation cancelled before it started
STATUSES = (STATUS_PENDING, STATUS_GENERATING, STATUS_DONE, STATUS_FAILED, STATUS_CANCELLED)


@dataclass
class Config:
    id: str
    name: str
    start_frame: Optional[str] = None       # path to start keypose
    end_frame: Optional[str] = None         # path to end keypose (optional / FLF)
    canvas_w: Optional[int] = None
    canvas_h: Optional[int] = None
    crop: dict = field(default_factory=dict)  # frame size / positioning / crop area
    prompt: str = ""
    negative_prompt: str = ""
    model_id: str = ""                      # ref into model_library.json
    settings: dict = field(default_factory=dict)  # model params (seed, duration, ...)
    created: str = ""
    updated: str = ""


@dataclass
class Result:
    id: str
    config_id: str
    status: str = STATUS_PENDING
    video_path: Optional[str] = None
    preview_gif: Optional[str] = None
    thumbnail: Optional[str] = None
    settings_snapshot: dict = field(default_factory=dict)  # IMMUTABLE provenance
    seed: Optional[int] = None
    cost_estimate: Optional[float] = None
    cost_actual: Optional[float] = None
    fps: Optional[float] = None
    frame_count: Optional[int] = None
    starred: bool = False
    deleted: bool = False                   # soft delete -> moved to data/bin/
    error: Optional[str] = None
    created: str = ""
    completed: Optional[str] = None


@dataclass
class Job:
    id: str
    result_id: str
    backend: str = ""                       # "replicate" | "comfyui"
    state: str = "queued"                   # queued | running | done | failed
    log: str = ""
    ext_id: Optional[str] = None            # replicate prediction id / comfy prompt id
    created: str = ""
    updated: str = ""
