"""Canonical paths for AnimGen.

AnimGen is a standalone tool: everything it owns lives under the repo root, with
runtime state under data/. It also references two EXTERNAL locations - a "source
project" (the Fighter sprite project) for keypose assets and the shipped-move
manifest, and a ComfyUI install for the local backend. Both default to siblings of
the repo and are overridable via environment variables:

    ANIMGEN_FIGHTER_ROOT   (default: ../Fighter)
    ANIMGEN_COMFY_DIR      (default: ../comfyui)
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent          # repo root (the AnimGen folder)
ANIMGEN_DIR = ROOT                               # backwards-compatible alias
PROJECTS_ROOT = ROOT.parent                      # e.g. C:/Programming

MODEL_LIBRARY_PATH = ROOT / "model_library.json"
WORKFLOWS_DIR = ROOT / "workflows"               # bundled local-backend templates
ENV_FILE = ROOT / ".env"                         # own token file (gitignored, optional)

DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "animgen.db"
RESULTS_DIR = DATA_DIR / "results"               # results/<config_id>/<result_id>.mp4
BIN_DIR = DATA_DIR / "bin"                       # soft-deleted results land here
EXPORTS_DIR = DATA_DIR / "exports"               # <name>_<timestamp>/ frame sets

# External references (overridable via env) -----------------------------------
COMFY_DIR = Path(os.environ.get("ANIMGEN_COMFY_DIR", str(PROJECTS_ROOT / "comfyui")))
COMFY_INPUT_DIR = COMFY_DIR / "input"            # LoadImage source dir

FIGHTER_ROOT = Path(os.environ.get("ANIMGEN_FIGHTER_ROOT", str(PROJECTS_ROOT / "Fighter")))
FIGHTER_ENV_FILE = FIGHTER_ROOT / ".env"         # token fallback (never copied into the repo)
ASSETS_DIR = FIGHTER_ROOT / "assets"             # keypose source images
FIGHTER_OUT = FIGHTER_ROOT / "out"               # existing takes (referenced, never moved)
GAME_SPRITES_MANIFEST = FIGHTER_ROOT / "scripts" / "game_sprites_manifest.json"


def ensure_dirs() -> None:
    """Create the runtime data directories if they don't exist."""
    for d in (DATA_DIR, RESULTS_DIR, BIN_DIR, EXPORTS_DIR):
        d.mkdir(parents=True, exist_ok=True)


def resolve_template(rel: str) -> Path:
    """Resolve a model's workflow_template: prefer the bundled copy under the repo,
    fall back to the external Fighter project for templates not shipped here."""
    bundled = ROOT / rel
    return bundled if bundled.exists() else FIGHTER_ROOT / rel
