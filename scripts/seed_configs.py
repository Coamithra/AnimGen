"""Seed the tool with the ~31 shipped moves from scripts/game_sprites_manifest.json.

Each manifest move becomes a config; its approved source take (the out/<take>.mp4 if
present) is attached as a STARRED 'done' result, with the move's retime preview GIF
as the thumbnail. Model + seed are best-effort from the source/note. External project
files are only REFERENCED, never moved (the tool stays purely additive). Idempotent:
re-running skips configs whose name already exists.

    PYTHONIOENCODING=utf-8 animgen/.venv/Scripts/python.exe animgen/scripts/seed_configs.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # animgen/

import paths  # noqa: E402
from store.db import Store  # noqa: E402
from store.models import STATUS_DONE  # noqa: E402


def _derive_take(source: str) -> str | None:
    parts = source.replace("\\", "/").split("/")
    if len(parts) >= 2 and parts[0] in ("frames", "out"):
        return parts[1]
    return None


def _derive_model(source: str, note: str) -> str:
    s = f"{source} {note}".lower()
    if "vidu" in s:
        return "vidu-q3-pro"
    if "seedance" in s:
        return "seedance-2.0-std"
    if source.startswith("out/") or any(k in s for k in ("flf", "vace", "derived")):
        return "local-flf-wan14b"
    return "seedance-2.0-std"


def seed(store: Store, manifest_path: Path, out_dir: Path, previews_dir: Path) -> int:
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    existing = {c.name for c in store.list_configs()}
    added = 0
    for m in manifest:
        move = m["move"]
        if move in existing:
            continue
        source = m.get("source", "")
        note = m.get("note", "")
        model_id = _derive_model(source, note)
        settings: dict = {"seed": 7}
        if model_id.startswith("seedance"):
            settings.update(duration=4, resolution="720p", aspect_ratio="1:1")

        take = _derive_take(source)
        video = None
        if take:
            cand = out_dir / f"{take}.mp4"
            if cand.exists():
                video = str(cand)
        preview = previews_dir / f"{move}.gif"
        preview_path = str(preview) if preview.exists() else None

        cfg = store.add_config(move, model_id=model_id, settings=settings,
                               prompt="", negative_prompt="")
        snapshot = {"move": move, "source": source, "note": note,
                    "model_id": model_id, "seed": 7, "loop": m.get("loop", False),
                    "provenance": "seeded from game_sprites_manifest.json"}
        store.add_result(cfg.id, status=STATUS_DONE, starred=True, seed=7,
                         video_path=video, preview_gif=preview_path,
                         thumbnail=preview_path, settings_snapshot=snapshot)
        added += 1
    return added


def main() -> None:
    paths.ensure_dirs()
    store = Store(paths.DB_PATH)
    added = seed(store, paths.GAME_SPRITES_MANIFEST, paths.FIGHTER_OUT,
                 paths.FIGHTER_OUT / "retime_previews")
    total = len(store.list_configs())
    store.close()
    print(f"seeded {added} shipped-move configs ({total} configs total now)")


if __name__ == "__main__":
    main()
