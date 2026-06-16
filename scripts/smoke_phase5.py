"""Phase 5 smoke test (offscreen, no spend).

Encodes a tiny real mp4, then exercises export: single take (flat folder), multiple
(parent + subfolders), skipped (no video), and verifies settings.txt carries the
immutable settings_snapshot. Also confirms the main window still builds.

    QT_QPA_PLATFORM=offscreen PYTHONIOENCODING=utf-8 \
        animgen/.venv/Scripts/python.exe animgen/scripts/smoke_phase5.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # animgen/

import av  # noqa: E402
import numpy as np  # noqa: E402

import paths  # noqa: E402

paths.SCRATCH_DIR = Path(tempfile.mkdtemp())  # keep untitled-project scratch out of data/

from pipeline import export  # noqa: E402
from store.project import Project  # noqa: E402
from store.models import STATUS_DONE, STATUS_PENDING  # noqa: E402


def _make_mp4(path: Path, n: int = 5) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    codec = "mpeg4"
    container = av.open(str(path), mode="w")
    stream = container.add_stream(codec, rate=8)
    stream.width, stream.height, stream.pix_fmt = 64, 64, "yuv420p"
    for i in range(n):
        arr = np.full((64, 64, 3), (i * 40) % 255, dtype=np.uint8)
        frame = av.VideoFrame.from_ndarray(arr, format="rgb24")
        for pkt in stream.encode(frame):
            container.mux(pkt)
    for pkt in stream.encode():
        container.mux(pkt)
    container.close()


def test_export() -> None:
    tmp = Path(tempfile.mkdtemp())
    dest = tmp / "exports"
    project = Project.new()
    shot = project.add_shot("kick_heavy", model_id="seedance-2.0-std",
                            prompt="fierce kick", settings={"seed": 7, "duration": 4})

    vid = tmp / "r1.mp4"
    _make_mp4(vid, n=5)
    snap = {"model_id": "seedance-2.0-std", "seed": 7, "prompt": "fierce kick",
            "settings": {"seed": 7, "duration": 4}}
    r1 = project.add_take(shot.id, status=STATUS_DONE, seed=7, video_path=str(vid),
                          settings_snapshot=snap, cost_estimate=0.72)

    # single -> flat folder with frames + settings.txt
    res = export.export_takes(project, [r1.id], dest_root=dest)
    folder = res["parent"]
    frames = sorted(folder.glob("frame_*.png"))
    assert len(frames) == 5, len(frames)
    txt = (folder / "settings.txt").read_text(encoding="utf-8")
    assert "settings_snapshot" in txt and '"seed": 7' in txt and "fierce kick" in txt
    assert "kick_heavy" in folder.name

    # multiple -> parent with one subfolder per take
    vid2 = tmp / "r2.mp4"; _make_mp4(vid2, n=3)
    r2 = project.add_take(shot.id, status=STATUS_DONE, video_path=str(vid2), settings_snapshot=snap)
    res2 = export.export_takes(project, [r1.id, r2.id], label="kick_heavy", dest_root=dest)
    subs = [p for p in res2["parent"].iterdir() if p.is_dir()]
    assert len(subs) == 2 and all((s / "settings.txt").exists() for s in subs)

    # skipped: a pending take with no video
    r3 = project.add_take(shot.id, status=STATUS_PENDING)
    res3 = export.export_takes(project, [r3.id], dest_root=dest)
    assert res3["parent"] is None and r3.id in res3["skipped"]
    print("export OK: single(flat)/multi(subfolders)/skipped, settings.txt snapshot")


def test_window_builds() -> None:
    from PySide6.QtWidgets import QApplication

    from ui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])  # noqa: F841
    project = Project.new()
    shot = project.add_shot("c", model_id="seedance-2.0-std")
    project.add_take(shot.id, status=STATUS_DONE)
    win = MainWindow(project)
    assert len(win.cards) == 1
    # export_current_view gathers ids without crashing (no video -> would no-op in UI)
    ids = []
    for card in win.cards.values():
        ids.extend(card._row_export_ids())
    assert len(ids) == 1

    # Unsaved-edit asterisks: a saved (clean) project shows no marker; editing an open
    # shot tab puts a '*' on that tab's text AND on the window title.
    p2 = Project.new()
    s2 = p2.add_shot("kick", model_id="seedance-2.0-std")
    p2.save_as(Path(tempfile.mkdtemp()) / "p2.animproj")   # titled + clean
    w2 = MainWindow(p2)
    assert not w2._has_unsaved_changes() and "*" not in w2.windowTitle()
    w2.open_shot(s2.id)
    tab = w2.shot_tabs[s2.id]
    idx = w2.tabs.indexOf(tab)
    assert w2.tabs.tabText(idx) == "kick", "a clean shot tab has no asterisk"
    tab.prompt.setPlainText("edited")
    assert tab.is_dirty() and w2.tabs.tabText(idx) == "kick*", "editing flags the tab text"
    assert w2._has_unsaved_changes() and "*" in w2.windowTitle(), "title reflects the dirty tab"
    # The discard/close guard must see the uncommitted tab edit, and Save must flush it
    # (otherwise the title advertises unsaved work the discard path would silently drop).
    assert w2._has_unsaved_edits(), "an uncommitted tab edit arms the save-prompt"
    assert w2.save_project(), "Save (titled project -> no dialog) succeeds"
    assert w2.project.get_shot(s2.id).prompt == "edited", "Save flushed the open tab"
    assert not tab.is_dirty() and w2.tabs.tabText(idx) == "kick", "saving clears the marker"
    assert not w2._has_unsaved_edits() and "*" not in w2.windowTitle()
    print("MainWindow OK: builds with export wiring, row ids gathered, dirty * propagates")


if __name__ == "__main__":
    test_export()
    test_window_builds()
    print("PHASE 5 SMOKE: PASS")
