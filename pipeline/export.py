"""Export takes as frame sets + a settings record.

Per spec: an export lands in a dedicated folder <name>_<timestamp>/ holding the
individual extracted PNG frames and a settings.txt documenting the generation
settings - the take's IMMUTABLE settings_snapshot - plus timestamps. A single take
exports flat into that folder; multiple takes (a whole row / a selection / the
current view) get one subfolder each under a parent <label>_<timestamp>/.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from paths import EXPORTS_DIR
from pipeline import extract


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _safe(name: str) -> str:
    s = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in (name or "")).strip("_")
    return s or "anim"


def _settings_text(shot, take) -> str:
    lines = [
        f"# Shot export:      {shot.name}",
        f"# exported:         {datetime.now().isoformat(timespec='seconds')}",
        f"# take id:          {take.id}",
        f"# take created:     {take.created}",
        f"# take completed:   {take.completed}",
        f"# status:           {take.status}",
    ]
    if take.seed is not None:
        lines.append(f"# seed:             {take.seed}")
    if take.cost_estimate is not None:
        lines.append(f"# cost_estimate:    ${take.cost_estimate}")
    if take.cost_actual is not None:
        lines.append(f"# cost_actual:      ${take.cost_actual}")
    lines += [
        f"# fps:              {take.fps}",
        f"# frame_count:      {take.frame_count}",
        f"# source video:     {take.video_path}",
        "",
        "## settings_snapshot (the exact settings that produced this take)",
        json.dumps(take.settings_snapshot, indent=2, ensure_ascii=False),
        "",
        "## shot (current values - may have changed since this take)",
        json.dumps({"name": shot.name, "model_id": shot.model_id,
                    "prompt": shot.prompt, "negative_prompt": shot.negative_prompt,
                    "settings": shot.settings, "start_frame": shot.start_frame,
                    "end_frame": shot.end_frame, "canvas": [shot.canvas_w, shot.canvas_h]},
                   indent=2, ensure_ascii=False),
    ]
    return "\n".join(lines) + "\n"


def _export_into(folder: Path, shot, take) -> int:
    folder.mkdir(parents=True, exist_ok=True)
    n = len(extract.extract_frames(take.video_path, folder, prefix="frame_"))
    (folder / "settings.txt").write_text(_settings_text(shot, take), encoding="utf-8")
    return n


def export_takes(project, take_ids: list, label: str = "selection",
                 dest_root: Path | str = EXPORTS_DIR) -> dict:
    """Export the given takes. Returns {parent, exported:[(folder,n_frames)], skipped:[ids]}."""
    takes = []
    for tid in take_ids:
        t = project.get_take(tid)
        if t and t.video_path and Path(t.video_path).exists():
            takes.append(t)
    skipped = [tid for tid in take_ids if project.get_take(tid)
               and tid not in {t.id for t in takes}]

    if not takes:
        return {"parent": None, "exported": [], "skipped": skipped}

    if len(takes) == 1:
        t = takes[0]
        shot = project.get_shot(t.shot_id)
        folder = Path(dest_root) / f"{_safe(shot.name)}_{_stamp()}_{t.id[:6]}"
        n = _export_into(folder, shot, t)
        return {"parent": folder, "exported": [(folder, n)], "skipped": skipped}

    parent = Path(dest_root) / f"{_safe(label)}_{_stamp()}"
    exported = []
    for t in takes:
        shot = project.get_shot(t.shot_id)
        sub = parent / f"{_safe(shot.name)}_{t.id[:6]}"
        n = _export_into(sub, shot, t)
        exported.append((sub, n))
    return {"parent": parent, "exported": exported, "skipped": skipped}
