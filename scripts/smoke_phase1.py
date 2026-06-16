"""Phase 1 smoke test: library load, project round-trip, offscreen GUI build.

Run headless with the animgen venv:
    QT_QPA_PLATFORM=offscreen PYTHONIOENCODING=utf-8 \
        animgen/.venv/Scripts/python.exe animgen/scripts/smoke_phase1.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # animgen/

import library  # noqa: E402
import paths  # noqa: E402

paths.SCRATCH_DIR = Path(tempfile.mkdtemp())  # keep untitled-project scratch out of data/

from store.project import Project  # noqa: E402
from store.models import STATUS_DONE  # noqa: E402


def test_library() -> None:
    lib = library.load_library()
    assert lib["models"], "no models in library"
    seed = library.get_model("seedance-2.0-std")
    assert seed and seed["replicate_model_id"] == "bytedance/seedance-2.0"
    cost = library.estimate_cost("seedance-2.0-std", {"duration": 4})
    assert cost is not None and abs(cost - 0.72) < 1e-6, cost   # default res (720p)
    # per-resolution pricing: 480p is cheaper than 720p for the same duration
    c720 = library.estimate_cost("seedance-2.0-std", {"duration": 4, "resolution": "720p"})
    c480 = library.estimate_cost("seedance-2.0-std", {"duration": 4, "resolution": "480p"})
    assert c720 is not None and c480 is not None and c480 < c720, (c480, c720)
    # Wan prices by resolution, Kling by mode (cost_by)
    w720 = library.estimate_cost("wan-2.7-i2v", {"duration": 4, "resolution": "720p"})
    w1080 = library.estimate_cost("wan-2.7-i2v", {"duration": 4, "resolution": "1080p"})
    assert w720 is not None and w1080 is not None and w720 < w1080, (w720, w1080)
    kstd = library.estimate_cost("kling-3.0", {"duration": 5, "mode": "standard"})
    kpro = library.estimate_cost("kling-3.0", {"duration": 5, "mode": "pro"})
    assert kstd is not None and kpro is not None and kstd < kpro, (kstd, kpro)
    assert library.estimate_cost("local-flf-wan14b", {}) == 0.0
    assert library.default_negative_prompt().startswith("camera pan")
    print(f"library OK: {len(lib['models'])} models; seedance 4s est ${cost:.2f}")


def test_project() -> None:
    p = Project.new()
    assert p.is_untitled and not p.dirty
    shot = p.add_shot(
        "kick_heavy", model_id="seedance-2.0-std", start_frame="assets/a.png",
        settings={"seed": 7, "duration": 4}, crop={"x": 1, "y": 2, "w": 3, "h": 4},
    )
    assert p.dirty, "add_shot should mark the project dirty"
    got = p.get_shot(shot.id)
    assert got.name == "kick_heavy" and got.settings["seed"] == 7 and got.crop["w"] == 3
    p.update_shot(shot.id, prompt="fierce kick", settings={"seed": 9, "duration": 5})
    got = p.get_shot(shot.id)
    assert got.prompt == "fierce kick" and got.settings["seed"] == 9

    take = p.add_take(
        shot.id, status=STATUS_DONE, seed=7,
        settings_snapshot={"model_id": "seedance-2.0-std", "seed": 7, "prompt": "fierce kick"},
    )
    assert p.get_take(take.id).settings_snapshot["seed"] == 7
    p.set_starred(take.id, True)
    assert p.list_takes(shot.id, starred_only=True)[0].id == take.id
    p.soft_delete_take(take.id)
    assert p.list_takes(shot.id) == []
    assert len(p.list_takes(shot.id, include_deleted=True)) == 1
    p.restore_take(take.id)
    assert len(p.list_takes(shot.id)) == 1
    assert p.used_model_ids() == ["seedance-2.0-std"]

    job = p.add_job(take.id, backend="replicate", state="queued")
    p.update_job(job.id, state="running", ext_id="pred_abc")
    assert p.get_job(job.id).ext_id == "pred_abc"

    # save -> load round-trip
    proj_path = Path(tempfile.mkdtemp()) / "round.animproj"
    p.save_as(proj_path)
    assert not p.dirty and proj_path.exists()
    q = Project.load(proj_path)
    assert len(q.list_shots()) == 1 and q.list_shots()[0].prompt == "fierce kick"
    assert len(q.list_takes()) == 1 and q.list_takes()[0].starred
    print("project OK: shot+take+job round-trip, snapshot, star/delete/restore, save/load")


def test_hybrid_persistence() -> None:
    """Shot edits buffer (dirty, not on disk); a finished take writes through at once."""
    proj_path = Path(tempfile.mkdtemp()) / "h.animproj"
    p = Project.new()
    s = p.add_shot("walk", model_id="local-flf-wan14b")
    p.save_as(proj_path)
    assert not p.dirty

    p.update_shot(s.id, name="walk_fwd")          # buffered authoring edit
    assert p.dirty
    p.add_take(s.id, status=STATUS_DONE)           # write-through to takes.json

    on_disk = json.loads(proj_path.read_text(encoding="utf-8"))
    assert on_disk["shots"][0]["name"] == "walk", "buffered shot edit must not be on disk"
    takes_doc = json.loads((p.assets_dir / "takes.json").read_text(encoding="utf-8"))
    assert len(takes_doc["takes"]) == 1, "finished take must auto-persist"
    print("hybrid persistence OK: shot edit buffered, take auto-persisted")


def test_gui_build() -> None:
    from PySide6.QtWidgets import QApplication

    from ui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])  # noqa: F841
    win = MainWindow(Project.new("Untitled"))
    win.show()
    app.processEvents()
    assert win.windowTitle().endswith("Animation Generator")
    # An untitled (never-saved) project carries the unsaved-changes marker.
    assert win._has_unsaved_changes() and win.windowTitle().startswith("Untitled*")
    print("GUI OK: MainWindow built + shown offscreen, untitled shows *")


if __name__ == "__main__":
    test_library()
    test_project()
    test_hybrid_persistence()
    test_gui_build()
    print("PHASE 1 SMOKE: PASS")
