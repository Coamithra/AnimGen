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

from pipeline import export, extract  # noqa: E402
from store.project import Project  # noqa: E402
from store.models import STATUS_DONE, STATUS_FAILED, STATUS_PENDING  # noqa: E402

# Keep the suite hermetic: a MainWindow built here must NOT fire the opt-in startup Replicate
# schema fetch. With the user's app_settings opting in AND a token resolvable via the source
# project's .env, that fetch is a real network call on a daemon thread that outlives the test
# and can race a later monkeypatch (it polluted the _ReplicateRefresher stop-flag test with a
# stray real-roster fetch). Stub it in-memory only - never touch data/app_settings.json.
from ui.main_window import MainWindow  # noqa: E402
MainWindow._maybe_refresh_schemas_on_startup = lambda self: None  # type: ignore[assignment,method-assign]


def _make_mp4(path: Path, n: int = 5) -> Path:
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
    return path


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


def test_extract_frames_wide_padding() -> None:
    """Frame filenames zero-pad to a width derived from the true decoded frame count, so a
    lexicographic sort always equals frame order. A flat {i:03d} pad scrambled takes past
    999 frames (frame_1000 sorting before frame_999); a take whose top index reaches 1000
    now widens to 4 digits, while a short take keeps the historic 3-digit floor."""
    tmp = Path(tempfile.mkdtemp())

    # Short take: the 3-digit floor is preserved (frame_000.png, the historic look).
    short_names = [p.name for p in extract.extract_frames(_make_mp4(tmp / "short.mp4", 5),
                                                           tmp / "short_frames")]
    assert short_names[0] == "frame_000.png", short_names[0]

    # A take whose top index reaches 1000 widens to 4 digits so the sort holds.
    paths = extract.extract_frames(_make_mp4(tmp / "long.mp4", 1001), tmp / "long_frames")
    assert len(paths) == 1001, len(paths)
    names = [p.name for p in paths]             # returned in frame order (0..1000)
    assert names[0] == "frame_0000.png" and names[-1] == "frame_1000.png", (names[0], names[-1])
    assert len({len(n) for n in names}) == 1, "padding must be fixed-width across all frames"
    assert names == sorted(names), "lexicographic sort must equal frame order"
    print("extract_frames OK: pad width tracks frame count (3-digit floor, 4 digits past 999)")


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


def test_tab_state_persistence() -> None:
    """Closing/opening tabs is captured into project.ui_state on save and rebuilt on the
    next open: closed fixed tabs stay closed, open shot tabs reopen, order + active tab are
    preserved, and a descriptor for a since-deleted shot is skipped (no crash). A project
    with no saved layout builds the default full fixed-tab set."""
    from PySide6.QtWidgets import QApplication

    from ui.main_window import MainWindow
    from ui.shot_tab import ShotTab

    app = QApplication.instance() or QApplication([])  # noqa: F841
    path = Path(tempfile.mkdtemp()) / "tabs.animproj"
    project = Project.new()
    shot = project.add_shot("kick", model_id="seedance-2.0-std", prompt="p")
    project.save_as(path)

    win = MainWindow(project)
    assert win.tabs.count() == 5, "default layout shows every fixed tab"

    # Close Assets + Model Library, open the shot tab, focus it, then Save.
    win._on_tab_close(win.tabs.indexOf(win.assets_tab))
    win._on_tab_close(win.tabs.indexOf(win.library_tab))
    win.open_shot(shot.id)
    shot_tab = win.shot_tabs[shot.id]
    win.tabs.setCurrentWidget(shot_tab)
    assert win.save_project(), "titled save succeeds (no dialog)"

    layout = win.project.ui_state["tabs"]
    keys = [(e["kind"], e.get("key") or e.get("id")) for e in layout]
    assert keys == [("fixed", "Shots"), ("fixed", "Queue"),
                    ("fixed", "ComfyUI Status"), ("shot", shot.id)], keys
    assert win.project.ui_state["active"] == win.tabs.indexOf(shot_tab)

    # Reopen from disk in a fresh window: the layout (closed fixed tabs, reopened shot tab,
    # order, active) is rebuilt.
    reopened = Project.load(path)
    win2 = MainWindow(reopened)
    titles = [win2.tabs.tabText(i) for i in range(win2.tabs.count())]
    assert titles == ["Shots", "Queue", "ComfyUI Status", "kick"], titles
    assert win2.tabs.indexOf(win2.assets_tab) < 0, "closed Assets tab stays closed"
    assert win2.tabs.indexOf(win2.library_tab) < 0, "closed Model Library tab stays closed"
    assert shot.id in win2.shot_tabs, "the open shot tab was reopened"
    assert isinstance(win2.tabs.currentWidget(), ShotTab), "active tab restored to the shot"

    # A descriptor that points at a since-deleted shot is silently skipped (no crash).
    reopened.delete_shot(shot.id)
    reopened.save()
    win3 = MainWindow(Project.load(path))
    assert shot.id not in win3.shot_tabs, "deleted-shot descriptor produces no tab"
    titles3 = [win3.tabs.tabText(i) for i in range(win3.tabs.count())]
    assert titles3 == ["Shots", "Queue", "ComfyUI Status"], titles3

    # A project with no saved ui_state falls back to the full default fixed-tab set.
    fresh = Project.new()
    fresh.add_shot("x", model_id="seedance-2.0-std")
    win4 = MainWindow(fresh)
    assert win4.tabs.count() == 5 and win4.tabs.tabText(0) == "Shots"
    print("MainWindow OK: open-tab layout captured on save + restored on open")


def test_tab_state_active_survives_skip() -> None:
    """The saved active tab is restored by identity, not raw tab position, so deleting an
    earlier tab's shot doesn't drift focus onto the wrong tab. Also exercises the take-tab
    descriptor round-trip (the 'take' kind, untested by the layout test above)."""
    from PySide6.QtWidgets import QApplication

    from ui.main_window import MainWindow
    from ui.take_player import TakePlayerTab

    app = QApplication.instance() or QApplication([])  # noqa: F841
    path = Path(tempfile.mkdtemp()) / "active.animproj"
    project = Project.new()
    a = project.add_shot("aaa", model_id="seedance-2.0-std")
    b = project.add_shot("bbb", model_id="seedance-2.0-std")
    take = project.add_take(a.id, status=STATUS_DONE)   # no video -> no decode thread
    project.save_as(path)

    win = MainWindow(project)
    win.open_shot(a.id)
    win.open_shot(b.id)
    win.open_take(take.id)                 # tab order: ...fixed..., a, b, take
    win.tabs.setCurrentWidget(win.shot_tabs[b.id])   # active is a NON-last tab
    assert win.save_project()

    # Reopen everything intact: the take viewer tab round-trips and focus lands on b.
    w2 = MainWindow(Project.load(path))
    assert take.id in w2.take_tabs and isinstance(w2.take_tabs[take.id], TakePlayerTab)
    assert a.id in w2.shot_tabs and b.id in w2.shot_tabs
    assert w2.tabs.currentWidget() is w2.shot_tabs[b.id], "active restored to shot b"

    # Delete shot a (an EARLIER descriptor than the active one) + its take, then reopen.
    # Position-based restore would now mis-point or fall back to Shots; identity-based
    # restore must keep focus on b.
    reop = Project.load(path)
    reop.delete_shot(a.id)
    reop.save()
    w3 = MainWindow(Project.load(path))
    assert a.id not in w3.shot_tabs, "deleted shot a is not reopened"
    assert take.id not in w3.take_tabs, "take orphaned by the shot delete is dropped"
    assert w3.tabs.currentWidget() is w3.shot_tabs[b.id], "focus stayed on b despite the skip"
    print("MainWindow OK: active tab restored by identity across a skipped earlier tab")


def test_tab_state_persists_on_close() -> None:
    """A tab rearrange on an otherwise-clean titled project is persisted at window close
    (no Save needed), gated on the layout actually changing so an unchanged close writes
    nothing, and suppressed when the project is untitled or the user Discards real edits.
    Also covers an active-tab-only switch (part of the layout) and confirms a no-op close
    skips the write. NB the Discard case asserts 'closeEvent wrote nothing' via mtime, not
    the real modal Discard wiring - headless can't drive the QMessageBox."""
    from PySide6.QtGui import QCloseEvent
    from PySide6.QtWidgets import QApplication

    from ui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])  # noqa: F841
    path = Path(tempfile.mkdtemp()) / "close.animproj"
    project = Project.new()
    shot = project.add_shot("kick", model_id="seedance-2.0-std", prompt="p")
    project.save_as(path)
    assert not project.ui_state, "fresh save records no ui_state (default layout)"
    base_mtime = path.stat().st_mtime_ns

    # Close a couple of fixed tabs on a clean project (a rearrange does NOT set dirty),
    # then close the window without saving.
    win = MainWindow(Project.load(path))
    assert not win._has_unsaved_edits(), "tab rearrange must not arm the save-prompt"
    win._on_tab_close(win.tabs.indexOf(win.assets_tab))
    win._on_tab_close(win.tabs.indexOf(win.library_tab))
    win.closeEvent(QCloseEvent())

    # The .animproj now carries the trimmed layout even though nothing was saved.
    reopened = Project.load(path)
    keys = [(e["kind"], e.get("key")) for e in reopened.ui_state["tabs"]]
    assert keys == [("fixed", "Shots"), ("fixed", "Queue"), ("fixed", "ComfyUI Status")], keys
    win2 = MainWindow(reopened)
    assert win2.tabs.indexOf(win2.assets_tab) < 0 and win2.tabs.indexOf(win2.library_tab) < 0

    # An unchanged close writes nothing (mtime untouched), so it can't churn the file.
    mtime_after = path.stat().st_mtime_ns
    win3 = MainWindow(Project.load(path))
    win3.closeEvent(QCloseEvent())
    assert path.stat().st_mtime_ns == mtime_after, "no-change close must not rewrite the file"

    # An active-tab-only switch is part of the layout: closing on it persists, and reopening
    # restores that tab as active.
    win5 = MainWindow(Project.load(path))
    win5.tabs.setCurrentWidget(win5.queue_tab)        # was Shots; switch to Queue, change nothing else
    assert not win5._has_unsaved_edits()
    win5.closeEvent(QCloseEvent())
    win6 = MainWindow(Project.load(path))
    assert win6.tabs.currentWidget() is win6.queue_tab, "reopens on the last-active tab"

    # Untitled project: nothing to write, no crash.
    untitled = MainWindow(Project.new())
    untitled._on_tab_close(untitled.tabs.indexOf(untitled.assets_tab))
    untitled.closeEvent(QCloseEvent())   # is_untitled -> skipped, no exception

    # Discard path: real authoring edits + a tab change, Discard at the prompt -> the
    # discarded shots must NOT be written back, and the layout change is dropped too.
    disc_path = Path(tempfile.mkdtemp()) / "disc.animproj"
    p2 = Project.new()
    p2.add_shot("a", model_id="seedance-2.0-std")
    p2.save_as(disc_path)
    disc_mtime = disc_path.stat().st_mtime_ns
    w = MainWindow(Project.load(disc_path))
    w.project.add_shot("ghost", model_id="seedance-2.0-std")   # buffered edit -> dirty
    w._on_tab_close(w.tabs.indexOf(w.assets_tab))               # + a layout change
    assert w._has_unsaved_edits()
    w._maybe_save_changes = lambda: True       # simulate the user picking Discard
    w.closeEvent(QCloseEvent())
    assert disc_path.stat().st_mtime_ns == disc_mtime, "Discard close must not write at all"
    assert "ghost" not in [s.name for s in Project.load(disc_path).list_shots()], "discarded edit not persisted"
    assert base_mtime != mtime_after          # sanity: the clean-close case really did write
    print("MainWindow OK: clean-close persists layout; no-change/untitled/Discard write nothing")


def test_tab_state_blank_tab_preserves_active() -> None:
    """Focusing a pristine unsaved blank shot tab (+ New Shot -> new_shot, never registered
    in shot_tabs so it maps to no descriptor) must not wipe the remembered active tab.
    _compute_tab_state re-points active at the prior descriptor, so a clean close keeps the
    on-disk active (Assets) instead of downgrading it to None/Shots (card #65)."""
    from PySide6.QtGui import QCloseEvent
    from PySide6.QtWidgets import QApplication

    from ui.main_window import MainWindow
    from ui.shot_tab import ShotTab

    app = QApplication.instance() or QApplication([])  # noqa: F841
    path = Path(tempfile.mkdtemp()) / "blank.animproj"
    project = Project.new()
    project.add_shot("kick", model_id="seedance-2.0-std", prompt="p")
    project.save_as(path)

    # Save with Assets active so the remembered active is a non-default (non-Shots) tab.
    win = MainWindow(project)
    win.tabs.setCurrentWidget(win.assets_tab)
    assert win.save_project()
    assets_idx = next(i for i, e in enumerate(win.project.ui_state["tabs"])
                      if e == {"kind": "fixed", "key": "Assets"})
    assert win.project.ui_state["active"] == assets_idx, "sanity: Assets recorded active"

    # Reopen (restores Assets active), then open a blank shot tab -> it takes focus but is
    # unregistered, so _compute_tab_state can't represent it as a descriptor.
    w2 = MainWindow(Project.load(path))
    assert w2.tabs.currentWidget() is w2.assets_tab, "restored on Assets"
    w2.new_shot()
    blank = w2.tabs.currentWidget()
    assert isinstance(blank, ShotTab) and blank not in w2.shot_tabs.values(), \
        "+ New Shot focuses a pristine, unregistered blank shot tab"
    assert not w2._has_unsaved_edits(), "a pristine blank tab is not an unsaved edit"
    state = w2._compute_tab_state()
    assert state["active"] is not None, "blank-tab focus must not null the active"
    assert state["tabs"][state["active"]] == {"kind": "fixed", "key": "Assets"}, state

    # A clean close persists that preserved active, so reopening lands on Assets, not Shots.
    w2.closeEvent(QCloseEvent())
    w3 = MainWindow(Project.load(path))
    assert w3.tabs.currentWidget() is w3.assets_tab, "reopens on Assets, not Shots (regression)"
    print("MainWindow OK: focusing a pristine blank shot tab preserves the remembered active")


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


def test_take_player_failure_message() -> None:
    """A failed take surfaces its recorded backend error in place of the video; a cancelled
    take shows a note; a pending/None take shows nothing special. Pure helper + viewer wiring."""
    from PySide6.QtWidgets import QApplication

    from store.models import (STATUS_CANCELLED, STATUS_FAILED, STATUS_GENERATING, Take)
    from ui.take_player import TakePlayerTab, failure_message

    err = "ReplicateError: ... Duration must be between 4 and 15 seconds, or -1."
    failed = Take(id="f", shot_id="s", status=STATUS_FAILED, error=err)
    msg = failure_message(failed)
    assert msg and err in msg and "failed to generate" in msg

    # A failed take with no recorded error still reads as a failure, not a blank.
    bare = failure_message(Take(id="b", shot_id="s", status=STATUS_FAILED))
    assert bare and "No error detail" in bare

    # Cancelled vs crash-interrupted get distinct notes; pending / None get nothing.
    assert "cancelled" in (failure_message(
        Take(id="c", shot_id="s", status=STATUS_CANCELLED)) or "").lower()
    assert "restarted" in (failure_message(
        Take(id="i", shot_id="s", status=STATUS_CANCELLED, interrupted=True)) or "").lower()
    assert failure_message(Take(id="p", shot_id="s", status=STATUS_GENERATING)) is None
    assert failure_message(None) is None

    # The viewer shows the error on its canvas (no video -> no decode thread spawned).
    app = QApplication.instance() or QApplication([])  # noqa: F841
    project = Project.new()
    shot = project.add_shot("kick", model_id="seedance-2.0-fast")
    take = project.add_take(shot.id, status=STATUS_FAILED, error=err)
    tab = TakePlayerTab(project, take.id)
    assert err in tab.canvas.text()
    tab.close_player()
    print("take_player OK: failure_message (failed error + cancelled note + viewer canvas)")


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


def test_generate_shot_missing_shot() -> None:
    """generate_shot is fed a deleted/unknown shot_id (generate_requested is a queued
    signal; the shot can vanish between emit and slot). Both guards - the top-level one
    and the one after the keyframe picker reloads the shot - must bail quietly: no
    AttributeError, no launch (_queue_take never reached), the cost gate never shown."""
    from PySide6.QtWidgets import QApplication

    from ui import main_window
    from ui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])  # noqa: F841
    project = Project.new()
    project.save_as(Path(tempfile.mkdtemp()) / "p.animproj")
    win = MainWindow(project)

    gate_shown, queued = [], []
    orig_confirm = main_window.confirm_launch
    main_window.confirm_launch = lambda *a, **k: gate_shown.append(True) or True
    win._queue_take = lambda *a, **k: queued.append(True)   # records any launch attempt
    try:
        # (a) top guard: an id that never existed, and one deleted between emit and slot.
        win.generate_shot("never-existed")
        ghost = project.add_shot("ghost", model_id="seedance-2.0-std", prompt="p")
        project.delete_shot(ghost.id)
        win.generate_shot(ghost.id)

        # (b) picker-reload guard: a shot with no start_frame, deleted while the keyframe
        # picker is open, so the re-fetch after import_asset returns None (line ~665).
        shot = project.add_shot("kick", model_id="seedance-2.0-std", prompt="p")
        orig_qfd, orig_import = main_window.QFileDialog, project.import_asset

        class _PickerThenDelete:
            @staticmethod
            def getOpenFileName(*a, **k):
                project.delete_shot(shot.id)        # vanishes while the dialog is open
                return ("frame.png", "")
        main_window.QFileDialog = _PickerThenDelete
        project.import_asset = lambda src: Path("frame.png")   # no real file needed
        try:
            win.generate_shot(shot.id)
        finally:
            main_window.QFileDialog, project.import_asset = orig_qfd, orig_import
    finally:
        main_window.confirm_launch = orig_confirm

    assert not gate_shown, "cost gate must never be shown for a missing shot"
    assert not queued, "no launch (_queue_take) for a missing shot"
    print("MainWindow OK: generate_shot on a missing/deleted shot is a quiet no-op (both guards)")


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
                            win.jobs._cancelled, win.jobs._stopping, win.jobs._requeue,
                            win.jobs._on_job_done)
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


def test_run_survives_deleted_signals() -> None:
    """A worker that emits after its _JobSignals C++ object was deleted out from under it
    (project / JobManager churn while a render is mid-flight) must NOT abort the process.
    GenerationJob._emit guards every emit, so a deleted source degrades to a dropped signal
    and run() still records the take terminally via write-through. Regression for the
    RuntimeError('Signal source has been deleted') -> C++ std::terminate crash (card #48)."""
    import shiboken6
    from PySide6.QtWidgets import QApplication

    from backends.jobs import GenerationJob, _JobSignals

    app = QApplication.instance() or QApplication([])  # noqa: F841
    project = Project.new()
    shot = project.add_shot("kick", model_id="seedance-2.0-std")
    take = project.add_take(shot.id, status=STATUS_PENDING,
                            settings_snapshot={"backend": "replicate"})

    signals = _JobSignals()
    done = []

    def runner(progress):
        progress("rendering", frac=0.5, label="step")  # emits while the source is still alive
        shiboken6.delete(signals)                      # tear down the C++ signals mid-render
        assert not shiboken6.isValid(signals)
        return {"video_path": "out.mp4"}

    job = GenerationJob(project, take.id, "replicate", runner, signals,
                        set(), set(), set(), lambda tid, st: done.append((tid, st)))
    job.run()                                          # the DONE/finished emits hit a dead source

    got = project.get_take(take.id)
    assert got.status == STATUS_DONE, got.status       # take recorded despite dead signals
    assert got.video_path == "out.mp4", got.video_path
    assert done == [(take.id, STATUS_DONE)], done      # finally still ran done_cb

    # Failure path: the runner raises *after* the signals die, so the except-branch emits
    # (status_changed->FAILED + failed) also hit a dead source and must no-op.
    take2 = project.add_take(shot.id, status=STATUS_PENDING,
                             settings_snapshot={"backend": "replicate"})
    signals2 = _JobSignals()
    done2 = []

    def failing_runner(progress):
        shiboken6.delete(signals2)
        raise RuntimeError("backend boom")

    job2 = GenerationJob(project, take2.id, "replicate", failing_runner, signals2,
                         set(), set(), set(), lambda tid, st: done2.append((tid, st)))
    job2.run()
    got2 = project.get_take(take2.id)
    assert got2.status == STATUS_FAILED, got2.status   # failure recorded despite dead signals
    assert done2 == [(take2.id, STATUS_FAILED)], done2

    # Early-transition failure (review finding #1): if the GENERATING-write itself blows up
    # before the inner try/finally, run()'s wrapper must still fire done_cb so the queue slot
    # is freed rather than leaked.
    take3 = project.add_take(shot.id, status=STATUS_PENDING,
                             settings_snapshot={"backend": "replicate"})
    done3 = []
    orig_update = project.update_take
    calls = {"n": 0}

    def flaky_update(take_id, **fields):
        if take_id == take3.id and calls["n"] == 0:    # blow up the GENERATING transition
            calls["n"] += 1
            raise RuntimeError("disk full")
        return orig_update(take_id, **fields)

    project.update_take = flaky_update
    try:
        job3 = GenerationJob(project, take3.id, "replicate", lambda p: {}, _JobSignals(),
                             set(), set(), set(), lambda tid, st: done3.append((tid, st)))
        job3.run()                                     # must not raise; must free the slot
    finally:
        project.update_take = orig_update
    assert done3 == [(take3.id, STATUS_FAILED)], done3  # slot freed despite early crash

    print("run survives deleted _JobSignals OK: emits no-op, take recorded, slot freed on early fail")


def test_refresher_survives_deleted_signals() -> None:
    """The Model Library's off-thread schema fetcher must not crash if its QObject is deleted
    out from under the daemon thread (window/app torn down while the fetch is still walking the
    roster) - the teardown emit degrades to a dropped signal. Regression for the phase-5
    exit-time SIGSEGV: an unguarded emit raised RuntimeError('Signal source has been deleted')
    on the EXIT path -> native crash at shutdown (same class as jobs.py's card #48 guard, but
    in model_library_window and on close). Also covers the cooperative stop flag."""
    import shiboken6
    from PySide6.QtWidgets import QApplication

    import library
    from backends import replicate_client
    from store import schema_cache
    from ui import model_library_window as mlw

    app = QApplication.instance() or QApplication([])  # noqa: F841
    models = [{"id": "m1", "replicate_model_id": "owner/m1"},
              {"id": "m2", "replicate_model_id": "owner/m2"}]

    # (a) token-missing branch: emits one `result` per model + `finished`, all on a dead source.
    ref = mlw._ReplicateRefresher(models)
    orig_token = replicate_client.load_token
    replicate_client.load_token = lambda: (_ for _ in ()).throw(RuntimeError("no token"))
    shiboken6.delete(ref)                              # tear down the C++ source mid-life
    assert not shiboken6.isValid(ref)
    try:
        ref._run()                                    # must NOT raise despite dead-source emits
    finally:
        replicate_client.load_token = orig_token

    # (b) success branch: the per-model `result` emit also hits a dead source and must no-op.
    ref2 = mlw._ReplicateRefresher(models[:1])
    saved = (replicate_client.load_token, replicate_client.get_input_schema,
             replicate_client.derive_capabilities, library.sync_model_capabilities,
             schema_cache.put)
    replicate_client.load_token = lambda: "tok"
    replicate_client.get_input_schema = lambda token, rid: ({"prompt": {}}, None)
    replicate_client.derive_capabilities = lambda props: {}
    library.sync_model_capabilities = lambda mid, caps: False
    schema_cache.put = lambda rid, props: None
    shiboken6.delete(ref2)
    try:
        ref2._run()                                   # success-path result emit hits a dead source
    finally:
        (replicate_client.load_token, replicate_client.get_input_schema,
         replicate_client.derive_capabilities, library.sync_model_capabilities,
         schema_cache.put) = saved

    # (c) cooperative stop: a set stop flag makes a live _run bail before any per-model fetch -
    # the schema fetch is never called (proves the stop flag, not an incidental network error),
    # no `result` is emitted, only the final `finished` fires with zero counts.
    fired, fetched = [], []
    ref3 = mlw._ReplicateRefresher(models)
    ref3.result.connect(lambda *a: fired.append(("result", a)))
    ref3.finished.connect(lambda *a: fired.append(("finished", a)))
    saved_c = (replicate_client.load_token, replicate_client.get_input_schema)
    replicate_client.load_token = lambda: "tok"
    replicate_client.get_input_schema = lambda token, rid: (fetched.append(rid) or ({}, None))
    ref3.stop()
    try:
        ref3._run()
        app.processEvents()
    finally:
        (replicate_client.load_token, replicate_client.get_input_schema) = saved_c
    # stop flag bailed before ref3 fetched any of ITS OWN models (assert on ref3's models, not
    # `fetched == []`, so a stray fetch from any other background refresher can't flake this).
    assert "owner/m1" not in fetched and "owner/m2" not in fetched, fetched
    assert fired == [("finished", (0, 0, 0))], fired    # no per-model result, just finished
    print("model_library OK: refresher survives deleted signals (token/success) + cooperative stop")


def test_save_as_rollback_on_write_failure() -> None:
    """A save_as whose document write raises must leave the project exactly as it was:
    identity (path/name/_assets_dir) unchanged, the in-memory path remap reversed, and the
    source assets still on disk -- no half-swapped identity, no lost scratch. Covers the
    untitled path (scratch is MOVED) and the already-saved path (assets are COPIED), and
    confirms the rolled-back project is still healthy enough to save once the write recovers."""
    tmp = Path(tempfile.mkdtemp())
    src = tmp / "kf.png"
    src.write_bytes(b"\x89PNG\r\n\x1a\n fake keyframe")

    def boom(*_a, **_k):
        raise PermissionError("simulated AV/indexer lock on the .animproj")

    # --- (A) untitled -> Save As: scratch is MOVED, so a failed write must move it back ---
    p = Project.new()
    asset = p.import_asset(src)
    shot = p.add_shot("kick", model_id="seedance-2.0-std", start_frame=str(asset))
    old_assets, old_name = p._assets_dir, p.name
    assert old_assets.exists() and asset.exists(), "scratch + imported asset present pre-save"

    target = tmp / "B.animproj"
    new_assets = Project._assets_for(target)
    p._write_project_file = boom                       # the document write fails
    try:
        p.save_as(target)
    except PermissionError:
        pass
    else:
        raise AssertionError("save_as must propagate the write failure")

    assert p.path is None, "identity not swapped: still untitled"
    assert p.name == old_name and p._assets_dir == old_assets, "name + _assets_dir restored"
    assert old_assets.exists() and asset.exists(), "scratch + asset preserved (not lost to the move)"
    assert not new_assets.exists(), "the moved scratch was moved back (no B.assets left behind)"
    assert not target.exists(), "no orphan .animproj written"
    assert p._shots[shot.id].start_frame == str(asset), "in-memory path remap was reversed"

    # The rolled-back project is still healthy: clearing the fault lets save_as succeed.
    del p._write_project_file
    p.save_as(target)
    assert p.path == target and target.exists(), "real save swaps identity + writes the doc"
    assert (new_assets / asset.name).exists(), "scratch moved on the successful save"
    assert not old_assets.exists(), "scratch consumed by the successful move"

    # --- (B) already-saved -> Save As elsewhere: assets are COPIED, original must survive ---
    saved_assets = p._assets_dir                       # the B.assets from the real save above
    other = tmp / "C.animproj"
    other_assets = Project._assets_for(other)
    p._write_project_file = boom
    try:
        p.save_as(other)
    except PermissionError:
        pass
    else:
        raise AssertionError("save_as must propagate the write failure (copy path)")

    assert p.path == target, "identity restored to the original saved path"
    assert saved_assets.exists() and (saved_assets / asset.name).exists(), \
        "original assets untouched (copied, not moved)"
    assert not other_assets.exists(), "the half-made copy was cleaned up"
    assert not other.exists(), "no orphan .animproj for the copy path"

    # --- (C) Save As OVER an occupied neighbour: a failed write must NOT wipe its assets ---
    occupied = tmp / "D.animproj"
    occ_assets = Project._assets_for(occupied)
    occ_assets.mkdir(parents=True)
    (occ_assets / "PRECIOUS.png").write_bytes(b"do not lose me")
    occupied.write_text("{}", encoding="utf-8")        # a different project already lives here
    p._write_project_file = boom
    try:
        p.save_as(occupied)
    except PermissionError:
        pass
    else:
        raise AssertionError("save_as must propagate the write failure (occupied target)")
    assert p.path == target, "identity restored; not swapped onto the occupied target"
    assert (occ_assets / "PRECIOUS.png").exists(), "neighbour's assets restored, not wiped"
    assert occupied.read_text(encoding="utf-8") == "{}", "neighbour's .animproj untouched"
    assert not list(occ_assets.parent.glob("D.assets.*.bak")), "move-aside backup cleaned up"
    del p._write_project_file

    # --- (a) partial failure: the .animproj write SUCCEEDS but takes.json fails. The orphan
    #         .animproj at the fresh path must be removed and the moved scratch put back. ---
    def boom_takes(*_a, **_k):
        raise PermissionError("simulated lock on takes.json")
    p2 = Project.new()
    a2 = p2.import_asset(src)
    p2.add_shot("punch", model_id="seedance-2.0-std", start_frame=str(a2))
    scratch2 = p2._assets_dir
    fresh = tmp / "E.animproj"
    fresh_assets = Project._assets_for(fresh)
    p2._write_takes_file = boom_takes                  # project file writes; takes file fails
    try:
        p2.save_as(fresh)
    except PermissionError:
        pass
    else:
        raise AssertionError("save_as must propagate a takes.json write failure")
    assert p2.path is None and p2._assets_dir == scratch2, "identity rolled back after takes failure"
    assert scratch2.exists() and (scratch2 / a2.name).exists(), "scratch moved back after takes failure"
    assert not fresh.exists(), "the .animproj written before the takes failure was removed"
    assert not fresh_assets.exists(), "no leftover assets dir at the fresh target"
    print("Project OK: save_as rolls back identity + assets on a failed document write")


def test_gif_export() -> None:
    """The pure GIF encoder writes a valid animated GIF; take_to_gif round-trips a real mp4."""
    from PIL import Image

    from pipeline import gif_export

    tmp = Path(tempfile.mkdtemp())

    # encode_gif: synthetic frames -> animated GIF with the right count, loop, per-frame ms.
    frames = [Image.new("RGB", (32, 24), (i * 60 % 256, 0, 0)) for i in range(4)]
    out = gif_export.encode_gif(frames, tmp / "synthetic.gif", fps=10.0)
    with Image.open(out) as g:
        assert g.is_animated and g.n_frames == 4, g.n_frames
        assert g.info.get("loop") == 0                       # infinite loop
        assert g.info.get("duration") == 100                 # 1000 / 10fps = 100ms

    # max_side downscales the longest edge.
    big = [Image.new("RGB", (200, 100), (0, i * 80 % 256, 0)) for i in range(3)]
    small = gif_export.encode_gif(big, tmp / "scaled.gif", fps=12.0, max_side=50)
    with Image.open(small) as g:
        assert max(g.size) <= 50, g.size

    # take_to_gif: decode a real mp4 and re-encode; frame count survives the round-trip.
    mp4 = _make_mp4(tmp / "clip.mp4", n=5)
    src_count = sum(1 for _ in extract.iter_frames(mp4))
    assert src_count == 5, src_count                         # the mp4 decodes to what we asked for
    gif = gif_export.take_to_gif(mp4, tmp / "clip.gif")
    with Image.open(gif) as g:
        assert g.is_animated and g.n_frames == src_count, (g.n_frames, src_count)

    # An empty frame list is a clean error, not a crash.
    try:
        gif_export.encode_gif([], tmp / "x.gif", fps=12.0)
    except ValueError:
        pass
    else:
        raise AssertionError("encode_gif([]) must raise ValueError")
    print("gif_export OK: encode_gif + take_to_gif (count, loop, duration, scale, empty)")


def test_take_player_gif_export() -> None:
    """The viewer's right-click menu exposes Save as GIF / Copy GIF, gated on a playable
    source; the encode + clipboard-file-URL paths work headless. No modal .exec()."""
    from PIL import Image
    from PySide6.QtWidgets import QApplication

    from pipeline import gif_export
    from ui.take_player import TakePlayerTab

    app = QApplication.instance() or QApplication([])  # noqa: F841
    tmp = Path(tempfile.mkdtemp())
    project = Project.new()
    shot = project.add_shot("round kick", model_id="seedance-2.0-std", prompt="x")
    take = project.add_take(shot.id, status=STATUS_DONE,
                            settings_snapshot={"model_id": "seedance-2.0-std"})
    tab = TakePlayerTab(project, take.id)               # no video -> no decode thread spawned

    # No playable source yet -> both entries present but disabled.
    menu = tab._build_context_menu()
    save = next(a for a in menu.actions() if a.text().startswith("Save as GIF"))
    copy = next(a for a in menu.actions() if a.text().startswith("Copy GIF"))
    assert not save.isEnabled() and not copy.isEnabled(), "GIF entries gated off without a source"

    # Point the take at a real mp4 -> entries enabled; default name derives from the shot.
    mp4 = _make_mp4(tmp / "clip.mp4", n=4)
    project.update_take(take.id, video_path=str(mp4))
    menu = tab._build_context_menu()
    save = next(a for a in menu.actions() if a.text().startswith("Save as GIF"))
    copy = next(a for a in menu.actions() if a.text().startswith("Copy GIF"))
    assert save.isEnabled() and copy.isEnabled(), "GIF entries enabled once a source exists"
    name = tab._default_gif_name()
    assert name.startswith("round_kick_") and name.endswith(".gif"), name

    # The tab's source feeds the encoder (the worker just wraps this) and writes a valid GIF.
    out = tmp / "manual.gif"
    gif_export.take_to_gif(tab._gif_source(), out)
    with Image.open(out) as g:
        assert g.is_animated

    # Clipboard path: a file URL goes on the clipboard and reads back (offscreen works).
    tab._set_clipboard_gif(str(out))
    urls = QApplication.clipboard().mimeData().urls()
    assert urls and Path(urls[0].toLocalFile()).name == "manual.gif", \
        [u.toString() for u in urls]
    # Release the mime data before the process tears down: the offscreen QPA's in-process
    # clipboard SIGSEGVs at interpreter exit if QMimeData is still on it (a Qt teardown
    # artifact of the offscreen platform, NOT the app - real Windows hands the file off to the
    # OS clipboard and persists it). Leaving it set is what made phase 5 exit 139 after PASS.
    QApplication.clipboard().clear()

    tab.close_player()
    print("TakePlayerTab OK: GIF menu (source-gated), default name, encode + clipboard URL")


def test_take_media_probe() -> None:
    """A finished take must carry fps/frame_count probed off its produced video - no backend
    runner reports them, so without the probe settings.txt prints 'fps: None' (card #80).
    Covers the pure helper, the GenerationJob stamping path, and the export line."""
    from PySide6.QtWidgets import QApplication

    from backends.jobs import GenerationJob, _JobSignals

    app = QApplication.instance() or QApplication([])  # noqa: F841
    tmp = Path(tempfile.mkdtemp())
    mp4 = _make_mp4(tmp / "clip.mp4", n=5)  # _make_mp4 encodes at rate=8 -> 8 fps, 5 frames

    # pure helper: fills missing, preserves already-set, no-ops on missing path / unreadable file
    assert extract.probe_media_fields(str(mp4)) == (8.0, 5)
    assert extract.probe_media_fields(str(mp4), fps=12.0, frame_count=2) == (12.0, 2)
    assert extract.probe_media_fields(None) == (None, None)
    assert extract.probe_media_fields(str(tmp / "missing.mp4")) == (None, None)  # best-effort

    # GenerationJob stamps the take from the runner's video_path
    project = Project.new()
    shot = project.add_shot("kick", model_id="seedance-2.0-std")
    take = project.add_take(shot.id, status=STATUS_PENDING,
                            settings_snapshot={"backend": "replicate", "seed": 7})
    job = GenerationJob(project, take.id, "replicate", lambda p: {"video_path": str(mp4)},
                        _JobSignals(), set(), set(), set(), lambda tid, st: None)
    job.run()
    got = project.get_take(take.id)
    assert got.status == STATUS_DONE, got.status
    assert got.fps == 8.0, got.fps
    assert got.frame_count == 5, got.frame_count

    # a take whose video can't be probed still completes cleanly (fields stay None, no crash)
    take2 = project.add_take(shot.id, status=STATUS_PENDING,
                             settings_snapshot={"backend": "replicate"})
    job2 = GenerationJob(project, take2.id, "replicate",
                         lambda p: {"video_path": str(tmp / "gone.mp4")},
                         _JobSignals(), set(), set(), set(), lambda tid, st: None)
    job2.run()
    got2 = project.get_take(take2.id)
    assert got2.status == STATUS_DONE and got2.fps is None and got2.frame_count is None

    # export now writes real fps + frame_count into settings.txt (was 'fps: None')
    res = export.export_takes(project, [take.id], dest_root=tmp / "exports")
    flines = (res["parent"] / "settings.txt").read_text(encoding="utf-8").splitlines()
    fps_line = next(l for l in flines if l.startswith("# fps:"))
    fc_line = next(l for l in flines if l.startswith("# frame_count:"))
    assert fps_line.split()[-1] == "8.0", fps_line
    assert fc_line.split()[-1] == "5", fc_line
    print("take media probe OK: fps/frame_count stamped (helper + job + export), best-effort")


def test_delete_shot_discard_preserves_takes() -> None:
    """Card H1: delete_shot is a BUFFERED (discardable) authoring edit, so it must not
    destructively purge the shot's takes from takes.json until the deletion is committed by
    save(). Three properties:
      (1) delete_shot -> reload (== Discard) brings BOTH the shot and its takes back;
      (2) a take write-through for a SURVIVING shot after a delete keeps the deleted shot's
          takes on disk (the concurrent-write-through leak);
      (3) delete_shot -> save -> reload drops the deleted shot's takes for good."""
    tmp = Path(tempfile.mkdtemp())
    path = tmp / "proj.animproj"

    p = Project.new()
    doomed = p.add_shot("doomed", model_id="seedance-2.0-std")
    keep = p.add_shot("keep", model_id="seedance-2.0-std")
    t1 = p.add_take(doomed.id, status=STATUS_DONE, seed=11, starred=True)
    t2 = p.add_take(doomed.id, status=STATUS_DONE, seed=12)
    p.save_as(path)   # titled + clean, takes.json on disk with both takes

    # --- (1) delete -> Discard (reload from disk) restores shot + its takes ---
    p.delete_shot(doomed.id)
    assert p.get_shot(doomed.id) is None, "shot gone from live view immediately"
    assert p.list_takes(doomed.id) == [], "deleted shot's takes gone from live view immediately"
    assert p.get_shot(keep.id) is not None and p.dirty, "surviving shot intact; deletion buffered dirty"

    reloaded = Project.load(path)   # == the Discard path (fresh load from untouched disk)
    assert reloaded.get_shot(doomed.id) is not None, "Discard brings the shot back from the .animproj"
    got = {t.id: t for t in reloaded.list_takes(doomed.id)}
    assert set(got) == {t1.id, t2.id}, "Discard restores BOTH takes from takes.json (no data loss)"
    assert got[t1.id].seed == 11 and got[t1.id].starred, "take metadata (seed/star) survives"

    # --- (2) a surviving shot's write-through must NOT flush away the held takes ---
    p.add_take(keep.id, status=STATUS_DONE, seed=99)   # write-through to takes.json
    mid = Project.load(path)
    assert {t.id for t in mid.list_takes(doomed.id)} == {t1.id, t2.id}, \
        "held takes survive a concurrent write-through until save"

    # --- (3) save commits the deletion: the takes are dropped for good ---
    p.save()
    assert not p._pending_take_purge, "save clears the pending-purge buffer"
    final = Project.load(path)
    assert final.get_shot(doomed.id) is None, "saved deletion removes the shot"
    assert final.list_takes(doomed.id) == [], "saved deletion drops its takes"
    assert {t.shot_id for t in final.list_takes()} == {keep.id}, "only the surviving shot's takes remain"
    print("delete_shot discard OK: takes survive Discard + write-through, dropped only on save (H1)")


if __name__ == "__main__":
    test_export()
    test_take_media_probe()
    test_extract_frames_wide_padding()
    test_window_builds()
    test_close_dirty_tab_guard()
    test_tab_state_persistence()
    test_tab_state_active_survives_skip()
    test_tab_state_persists_on_close()
    test_tab_state_blank_tab_preserves_active()
    test_format_generation_settings()
    test_take_player_failure_message()
    test_snapshot_includes_framing()
    test_generate_shot_missing_shot()
    test_take_player_settings_panel()
    test_gif_export()
    test_take_player_gif_export()
    test_runner_self_cancel_during_submit()
    test_run_survives_deleted_signals()
    test_refresher_survives_deleted_signals()
    test_save_as_rollback_on_write_failure()
    test_delete_shot_discard_preserves_takes()
    print("PHASE 5 SMOKE: PASS")
