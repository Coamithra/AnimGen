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


def test_close_dirty_tab_guard() -> None:
    """Closing a shot tab with uncommitted edits must prompt; Cancel keeps it, Discard
    drops the edits, Save flushes them to the buffer. A clean tab closes with no prompt."""
    from PySide6.QtWidgets import QApplication, QMessageBox

    from ui import main_window
    from ui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])  # noqa: F841
    project = Project.new()
    s = project.add_shot("kick", model_id="seedance-2.0-std")
    project.save_as(Path(tempfile.mkdtemp()) / "p.animproj")
    win = MainWindow(project)

    # Stub the modal so it never blocks; count prompts per block and return a chosen button.
    # The counter resets each block so every assertion is a self-contained delta, and the
    # real QMessageBox.question is restored afterwards so the stub can't leak into later tests.
    asked = {"n": 0}
    Btn = QMessageBox.StandardButton
    orig_question = main_window.QMessageBox.question

    def stub(choice):
        def _q(*_a, **_k):
            asked["n"] += 1
            return choice
        return _q

    try:
        # Clean tab -> no prompt, closes straight away.
        win.open_shot(s.id)
        idx = win.tabs.indexOf(win.shot_tabs[s.id])
        asked["n"] = 0
        main_window.QMessageBox.question = stub(Btn.Cancel)
        win._on_tab_close(idx)
        assert asked["n"] == 0, "a clean tab closes without a prompt"
        assert s.id not in win.shot_tabs, "clean tab actually closed"

        # Dirty tab + Cancel -> prompted, tab stays open, edit preserved.
        win.open_shot(s.id)
        tab = win.shot_tabs[s.id]
        tab.prompt.setPlainText("edited-cancel")
        asked["n"] = 0
        main_window.QMessageBox.question = stub(Btn.Cancel)
        win._on_tab_close(win.tabs.indexOf(tab))
        assert asked["n"] == 1 and s.id in win.shot_tabs, "Cancel keeps the dirty tab open"
        assert tab.is_dirty(), "Cancel preserves the uncommitted edit"

        # Dirty tab + Discard -> prompted, tab closes, edit dropped (buffer unchanged).
        asked["n"] = 0
        main_window.QMessageBox.question = stub(Btn.Discard)
        win._on_tab_close(win.tabs.indexOf(tab))
        assert asked["n"] == 1 and s.id not in win.shot_tabs, "Discard closes the tab"
        assert project.get_shot(s.id).prompt == "", "Discard did not commit the edit"

        # Dirty tab + Save -> prompted, tab closes, edit flushed into the project buffer.
        win.open_shot(s.id)
        tab = win.shot_tabs[s.id]
        tab.prompt.setPlainText("edited-save")
        asked["n"] = 0
        main_window.QMessageBox.question = stub(Btn.Save)
        win._on_tab_close(win.tabs.indexOf(tab))
        assert asked["n"] == 1 and s.id not in win.shot_tabs, "Save closes the tab"
        assert project.get_shot(s.id).prompt == "edited-save", "Save flushed the edit to the buffer"
    finally:
        main_window.QMessageBox.question = orig_question
    print("MainWindow OK: close-dirty-tab guard (clean/Cancel/Discard/Save)")


def test_format_generation_settings() -> None:
    """The take-viewer settings formatter renders a full snapshot and degrades cleanly."""
    from store.models import Take
    from ui.take_player import format_generation_settings

    t = Take(id="abc123", shot_id="s1", seed=7, settings_snapshot={
        "model_id": "seedance-2.0-std", "backend": "replicate", "prompt": "fierce kick",
        "negative_prompt": "blurry", "canvas": [1254, 704], "crop": {"aspect": "16:9"},
        "settings": {"seed": 7, "duration": 4, "mode": "std"}})
    txt = format_generation_settings(t)
    assert "Seedance 2.0 (Std)" in txt          # model_id resolved to display name
    assert "1254 x 704" in txt and "16:9" in txt  # framing now travels in the snapshot
    assert "Seed:      7" in txt                  # seed lifted out of the params dump
    assert "fierce kick" in txt and "blurry" in txt
    assert "duration: 4" in txt and "mode: std" in txt
    assert "seed: 7" not in txt                   # not duplicated inside Parameters

    # An unframed shot snapshots canvas [None, None] -> the Canvas line is suppressed,
    # not rendered as "None x None" (and a malformed 1-element canvas can't IndexError).
    sparse = format_generation_settings(Take(id="y", shot_id="s", settings_snapshot={
        "model_id": "seedance-2.0-std", "canvas": [None, None], "prompt": "p"}))
    assert "Canvas:" not in sparse and "None x None" not in sparse

    empty = format_generation_settings(Take(id="x", shot_id="s"))
    assert "No generation settings" in empty
    print("take_player OK: format_generation_settings (full + sparse + empty)")


def test_snapshot_includes_framing() -> None:
    """generate_shot freezes canvas + crop into the take's immutable snapshot, so framing
    is preserved per take even after the shot is re-framed (the export/panel read it)."""
    from PySide6.QtWidgets import QApplication

    import library
    from ui import main_window
    from ui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])  # noqa: F841
    project = Project.new()
    shot = project.add_shot("kick", model_id="seedance-2.0-std", prompt="p",
                            settings={"seed": 5})
    aspects = library.aspect_ratios(shot.model_id)
    crop = {"aspect": aspects[0] if aspects else None,
            "start": {"scale": 1.0, "cx": 0.5, "cy": 0.5}}
    project.update_shot(shot.id, canvas_w=1254, canvas_h=704, crop=crop,
                        start_frame="x.png")
    project.save_as(Path(tempfile.mkdtemp()) / "p.animproj")
    win = MainWindow(project)

    orig_confirm = main_window.confirm_launch
    orig_enqueue = win.jobs.enqueue
    main_window.confirm_launch = lambda *a, **k: True   # auto-confirm the launch gate
    win.jobs.enqueue = lambda *a, **k: None             # don't actually render
    try:
        win.generate_shot(shot.id)
    finally:
        main_window.confirm_launch = orig_confirm
        win.jobs.enqueue = orig_enqueue

    snap = project.list_takes(shot.id)[-1].settings_snapshot
    assert snap["canvas"] == [1254, 704], snap.get("canvas")
    assert snap["crop"] == crop, snap.get("crop")
    print("MainWindow OK: snapshot carries canvas + crop framing")


def test_take_player_settings_panel() -> None:
    """The viewer's ⚙ button and right-click 'Show generation settings' both reveal the
    docked panel; it's hidden by default and toggles off again. No modal .exec()."""
    from PySide6.QtWidgets import QApplication

    from ui.take_player import TakePlayerTab

    app = QApplication.instance() or QApplication([])  # noqa: F841
    project = Project.new()
    shot = project.add_shot("kick", model_id="seedance-2.0-std", prompt="fierce")
    take = project.add_take(shot.id, status=STATUS_DONE, seed=7, settings_snapshot={
        "model_id": "seedance-2.0-std", "prompt": "fierce", "settings": {"seed": 7}})
    tab = TakePlayerTab(project, take.id)          # no video -> no decode thread spawned

    assert tab.settings_dock.isHidden(), "panel hidden by default"
    tab.show_settings()                            # the shared reveal path (button + menu)
    assert not tab.settings_dock.isHidden()
    assert "fierce" in tab.settings_panel.toPlainText()
    tab._on_settings_toggled(False)                # unchecking the button hides it
    assert tab.settings_dock.isHidden()

    menu = tab._build_context_menu()               # built without exec()
    acts = [a for a in menu.actions() if "generation settings" in a.text().lower()]
    assert len(acts) == 1, [a.text() for a in menu.actions()]
    acts[0].trigger()
    assert not tab.settings_dock.isHidden(), "context menu reveals the panel"
    tab.close_player()
    print("TakePlayerTab OK: settings dock toggles via button + context menu")


def test_runner_self_cancel_during_submit() -> None:
    """The replicate runner's on_submit must self-cancel when a stop was requested during
    the create-POST window (before backend_job_id existed), so the take lands CANCELLED and
    spend halts. Exercises the real ui.main_window._make_runner wiring with framing +
    replicate_client patched (hermetic - no keyposes, no network, no spend)."""
    from PySide6.QtWidgets import QApplication

    from backends.jobs import GenerationJob
    from ui import main_window
    from ui.main_window import MainWindow
    from store.models import STATUS_CANCELLED, STATUS_PENDING

    app = QApplication.instance() or QApplication([])  # noqa: F841
    project = Project.new()
    shot = project.add_shot("kick", model_id="seedance-2.0-std")
    win = MainWindow(project)
    take = project.add_take(shot.id, status=STATUS_PENDING,
                            settings_snapshot={"backend": "replicate"})

    cancels = []
    saved = (main_window.framing.render_keyposes,
             main_window.replicate_client.generate,
             main_window.replicate_client.cancel_prediction)
    main_window.framing.render_keyposes = lambda s, d: ("start.png", "end.png")
    main_window.replicate_client.cancel_prediction = lambda pid, token=None: cancels.append(pid)

    def fake_generate(rid, *, on_submit=None, **kw):
        win.jobs._stopping.add(take.id)   # stop requested while the create-POST is in flight
        on_submit("pred_post_window")     # create-POST returns -> on_submit records id + self-cancels
        # the poll loop would then see status "canceled" and raise out of run_prediction:
        raise main_window.replicate_client.ReplicateError("canceled")
    main_window.replicate_client.generate = fake_generate

    try:
        model = {"backend": "replicate", "replicate_model_id": "owner/model"}
        runner = win._make_runner(model, shot, {}, take.id)
        job = GenerationJob(project, take.id, "replicate", runner, win.jobs._signals,
                            win.jobs._cancelled, win.jobs._stopping)
        job.run()
        app.processEvents()
    finally:
        (main_window.framing.render_keyposes,
         main_window.replicate_client.generate,
         main_window.replicate_client.cancel_prediction) = saved

    got = project.get_take(take.id)
    assert cancels == ["pred_post_window"], cancels    # the real on_submit fired the cancel
    assert got.backend_job_id == "pred_post_window"    # id recorded before the self-cancel
    assert got.status == STATUS_CANCELLED, got.status  # not DONE - spend halted
    assert take.id not in win.jobs._stopping           # cleared in GenerationJob's finally
    print("runner self-cancel OK: real on_submit cancels during create-POST window")


if __name__ == "__main__":
    test_export()
    test_window_builds()
    test_close_dirty_tab_guard()
    test_format_generation_settings()
    test_snapshot_includes_framing()
    test_take_player_settings_panel()
    test_runner_self_cancel_during_submit()
    print("PHASE 5 SMOKE: PASS")
