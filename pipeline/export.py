"""Export results as frame sets + a settings record.

Per spec: an export lands in a dedicated folder <name>_<timestamp>/ holding the
individual extracted PNG frames and a settings.txt documenting the generation
settings - the result's IMMUTABLE settings_snapshot - plus timestamps. A single
result exports flat into that folder; multiple results (a whole row / a selection /
the current view) get one subfolder each under a parent <label>_<timestamp>/.
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


def _settings_text(config, result) -> str:
    lines = [
        f"# Animation export: {config.name}",
        f"# exported:        {datetime.now().isoformat(timespec='seconds')}",
        f"# result id:       {result.id}",
        f"# result created:  {result.created}",
        f"# result completed:{result.completed}",
        f"# status:          {result.status}",
    ]
    if result.seed is not None:
        lines.append(f"# seed:            {result.seed}")
    if result.cost_estimate is not None:
        lines.append(f"# cost_estimate:   ${result.cost_estimate}")
    if result.cost_actual is not None:
        lines.append(f"# cost_actual:     ${result.cost_actual}")
    lines += [
        f"# fps:             {result.fps}",
        f"# source video:    {result.video_path}",
        "",
        "## settings_snapshot (the exact settings that produced this result)",
        json.dumps(result.settings_snapshot, indent=2, ensure_ascii=False),
        "",
        "## config (current values - may have changed since this result)",
        json.dumps({"name": config.name, "model_id": config.model_id,
                    "prompt": config.prompt, "negative_prompt": config.negative_prompt,
                    "settings": config.settings, "start_frame": config.start_frame,
                    "end_frame": config.end_frame, "canvas": [config.canvas_w, config.canvas_h]},
                   indent=2, ensure_ascii=False),
    ]
    return "\n".join(lines) + "\n"


def _export_into(folder: Path, config, result) -> int:
    folder.mkdir(parents=True, exist_ok=True)
    n = len(extract.extract_frames(result.video_path, folder, prefix="frame_"))
    (folder / "settings.txt").write_text(_settings_text(config, result), encoding="utf-8")
    return n


def export_results(store, result_ids: list, label: str = "selection",
                   dest_root: Path | str = EXPORTS_DIR) -> dict:
    """Export the given results. Returns {parent, exported:[(folder,n_frames)], skipped:[ids]}."""
    results = []
    for rid in result_ids:
        r = store.get_result(rid)
        if r and r.video_path and Path(r.video_path).exists():
            results.append(r)
    skipped = [rid for rid in result_ids if store.get_result(rid)
               and rid not in {r.id for r in results}]

    if not results:
        return {"parent": None, "exported": [], "skipped": skipped}

    if len(results) == 1:
        r = results[0]
        cfg = store.get_config(r.config_id)
        folder = Path(dest_root) / f"{_safe(cfg.name)}_{_stamp()}_{r.id[:6]}"
        n = _export_into(folder, cfg, r)
        return {"parent": folder, "exported": [(folder, n)], "skipped": skipped}

    parent = Path(dest_root) / f"{_safe(label)}_{_stamp()}"
    exported = []
    for r in results:
        cfg = store.get_config(r.config_id)
        sub = parent / f"{_safe(cfg.name)}_{r.id[:6]}"
        n = _export_into(sub, cfg, r)
        exported.append((sub, n))
    return {"parent": parent, "exported": exported, "skipped": skipped}
