"""Recover orphaned take media: .mp4 files sitting in <project>.assets/takes/ that no
take record in takes.json references (e.g. a render that wrote its .mp4 but whose take
record never got persisted because the app/ComfyUI died mid-write).

Collects every orphan into a new shot (default name "recovered") as DONE takes so they
become visible in the project again. Take id is preserved from the .mp4 filename (the
app's own convention is take-id == file basename), so existing thumbs/<id>.png stay
valid; missing thumbnails are regenerated. Files that won't decode are reported and
NOT added (a broken file isn't a playable take).

The settings_snapshot is left empty: we have no provenance for an orphan, so we record
the honest unknown rather than inventing one. created/completed are stamped from the
.mp4 mtime so the recovered takes sort into roughly their real generation order.

Usage (from repo root, with the venv python so av/PIL are present):
    PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe scripts/_recover_orphan_takes.py \
        --project data/Biker.animproj [--shot-name recovered] [--dry-run]
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

# repo root on sys.path (Windows-style path, per CLAUDE.md rule #6)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from store.project import Project          # noqa: E402
from store.models import Take, STATUS_DONE  # noqa: E402
from pipeline import extract               # noqa: E402

_MEDIA_FIELDS = ("video_path", "thumbnail", "preview_gif")


def _iso_mtime(p: Path) -> str:
    return datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="seconds")


def find_orphans(project: Project) -> list[Path]:
    assets = project.assets_dir
    takes_dir = assets / "takes"
    if not takes_dir.exists():
        return []
    referenced: set[str] = set()
    for t in project.list_takes(include_deleted=True):
        for f in _MEDIA_FIELDS:
            val = getattr(t, f, None)
            if val:
                referenced.add(os.path.normcase(os.path.basename(val)))
    orphans = [p for p in sorted(takes_dir.glob("*.mp4"))
               if os.path.normcase(p.name) not in referenced]
    return orphans


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    ap.add_argument("--shot-name", default="recovered")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    project = Project.load(Path(args.project))
    assets = project.assets_dir
    thumbs_dir = assets / "thumbs"

    orphans = find_orphans(project)
    print(f"Found {len(orphans)} orphaned .mp4 file(s) in {assets / 'takes'}")
    if not orphans:
        return 0

    # Decode-probe every orphan; only decodable files become takes.
    recoverable: list[tuple[Path, dict]] = []
    broken: list[tuple[Path, str]] = []
    for p in orphans:
        try:
            recoverable.append((p, extract.video_info(p)))
        except Exception as e:                       # corrupt / truncated mp4
            broken.append((p, f"{type(e).__name__}: {e}"))

    print(f"  decodable: {len(recoverable)}   undecodable: {len(broken)}")
    for p, why in broken:
        print(f"    [SKIP corrupt] {p.name}  ({why})")

    if args.dry_run:
        print("\n--dry-run: no changes written.")
        return 0

    shot = project.add_shot(args.shot_name)
    print(f"\nCreated shot '{shot.name}' ({shot.id})")

    thumbs_made = 0
    for p, info in recoverable:
        tid = p.stem
        thumb = thumbs_dir / f"{tid}.png"
        if not thumb.exists():
            try:
                if extract.make_thumbnail(p, thumb):
                    thumbs_made += 1
            except Exception as e:
                print(f"    [thumb failed] {tid}: {e}")
                thumb = None
        stamp = _iso_mtime(p)
        take = Take(
            id=tid,
            shot_id=shot.id,
            status=STATUS_DONE,
            video_path=str(p),                       # absolute in-memory; relativized on write
            thumbnail=str(thumb) if thumb and Path(thumb).exists() else None,
            settings_snapshot={},                    # unknown provenance for an orphan
            fps=info.get("fps"),
            frame_count=info.get("frames"),
            interrupted=False,
            created=stamp,
            completed=stamp,
        )
        project._takes[tid] = take                   # preserve id == filename (add_take would mint a new one)

    project.save()
    print(f"Added {len(recoverable)} recovered take(s); generated {thumbs_made} new thumbnail(s).")
    print(f"Saved {project.path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
