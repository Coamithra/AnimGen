"""Seed the starter project with the ~31 shipped moves from game_sprites_manifest.json.

Each manifest move becomes a Shot (start/end keyposes = first/last frame of its PNG
sequence); its approved source take (the out/<take>.mp4 if present) is attached as a
STARRED 'done' Take, with the move's retime preview GIF as the thumbnail. Model + seed
are best-effort from the source/note. Keyframes are IMPORTED into the project's .assets/
(copied; originals left untouched), while takes/preview GIFs stay external references.
Writes data/Fighter.animproj; idempotent - re-running adds only shots whose name doesn't
already exist.

    PYTHONIOENCODING=utf-8 animgen/.venv/Scripts/python.exe animgen/scripts/seed_configs.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # animgen/

import paths  # noqa: E402
from store.project import Project  # noqa: E402
from store.models import STATUS_DONE  # noqa: E402


def _derive_take(source: str) -> str | None:
    parts = source.replace("\\", "/").split("/")
    if len(parts) >= 2 and parts[0] in ("frames", "out"):
        return parts[1]
    return None


def _resolve_frame(root: Path, source: str, fname: str) -> str | None:
    """Resolve a manifest frame's `src`. Normally it sits under the move's take dir
    (<FIGHTER_ROOT>/<source>/<fname>); a few (e.g. a canonical FLF keypose) are given
    relative to the project root instead (<FIGHTER_ROOT>/<fname>)."""
    for p in (root / source / fname, root / fname):
        if p.exists():
            return str(p)
    return None


def _keyposes(root: Path, source: str, frames: list) -> tuple[str | None, str | None]:
    """First/last frame of the move's PNG sequence -> the FLF start/end keyposes."""
    if not frames:
        return None, None
    start = _resolve_frame(root, source, frames[0]["src"])
    end = _resolve_frame(root, source, frames[-1]["src"])
    return start, end


def _derive_model(source: str, note: str) -> str:
    s = f"{source} {note}".lower()
    if "vidu" in s:
        return "vidu-q3-pro"
    if "seedance" in s:
        return "seedance-2.0-std"
    if source.startswith("out/") or any(k in s for k in ("flf", "vace", "derived")):
        return "local-flf-wan14b"
    return "seedance-2.0-std"


def seed(project: Project, manifest_path: Path, out_dir: Path, previews_dir: Path) -> int:
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    root = out_dir.parent  # FIGHTER_ROOT; manifest frame paths are relative to it
    existing = {s.name for s in project.list_shots()}
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

        start_frame, end_frame = _keyposes(root, source, m.get("frames") or [])
        # Import the keyframes into the project's .assets/ (copies, leaves originals).
        start_asset = str(project.import_asset(start_frame)) if start_frame else None
        end_asset = str(project.import_asset(end_frame)) if end_frame else None
        take_folder = _derive_take(source)
        video = None
        if take_folder:
            cand = out_dir / f"{take_folder}.mp4"
            if cand.exists():
                video = str(cand)
        preview = previews_dir / f"{move}.gif"
        preview_path = str(preview) if preview.exists() else None

        shot = project.add_shot(move, model_id=model_id, settings=settings,
                                prompt="", negative_prompt="",
                                start_frame=start_asset, end_frame=end_asset)
        snapshot = {"move": move, "source": source, "note": note,
                    "model_id": model_id, "seed": 7, "loop": m.get("loop", False),
                    "provenance": "seeded from game_sprites_manifest.json"}
        project.add_take(shot.id, status=STATUS_DONE, starred=True, seed=7,
                         video_path=video, preview_gif=preview_path,
                         thumbnail=preview_path, settings_snapshot=snapshot)
        added += 1
    return added


def main() -> None:
    paths.ensure_dirs()
    if paths.DEFAULT_PROJECT.exists():
        project = Project.load(paths.DEFAULT_PROJECT)
    else:
        project = Project.new("Fighter")
    added = seed(project, paths.GAME_SPRITES_MANIFEST, paths.FIGHTER_OUT,
                 paths.FIGHTER_OUT / "retime_previews")
    if project.is_untitled:
        project.save_as(paths.DEFAULT_PROJECT)
    else:
        project.save()
    total = len(project.list_shots())
    print(f"seeded {added} shipped-move shots -> {project.path} ({total} shots total now)")


if __name__ == "__main__":
    main()
