"""Phase 4 smoke test (offscreen, no spend).

Covers bin/restore (project-owned moved, external left in place), the takes view's
filter/star/delete, shot-card expansion, and the main window building cards.

    QT_QPA_PLATFORM=offscreen PYTHONIOENCODING=utf-8 \
        animgen/.venv/Scripts/python.exe animgen/scripts/smoke_phase4.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # animgen/

from PIL import Image  # noqa: E402

import paths  # noqa: E402

paths.SCRATCH_DIR = Path(tempfile.mkdtemp())  # keep untitled-project scratch out of data/

from store.project import Project  # noqa: E402
from store.models import (  # noqa: E402
    STATUS_DONE, STATUS_FAILED, STATUS_GENERATING, STATUS_PENDING,
)


def _png(path: Path, color=(0, 200, 0)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (16, 16), color).save(path)


def test_bin_restore() -> None:
    from pipeline import takes_io

    project = Project.new()
    shot = project.add_shot("c", model_id="seedance-2.0-std")

    # project-owned file (under the project's assets dir) -> moved to bin
    owned = project.takes_dir / "r.mp4"
    owned.write_bytes(b"video")
    t1 = project.add_take(shot.id, status=STATUS_DONE, video_path=str(owned))
    takes_io.move_to_bin(project.get_take(t1.id), project)
    g1 = project.get_take(t1.id)
    assert g1.deleted and not owned.exists()
    assert (project.bin_dir / t1.id / "r.mp4").exists()
    assert Path(g1.video_path).exists()
    # restore
    takes_io.restore_from_bin(project.get_take(t1.id), project)
    g1 = project.get_take(t1.id)
    assert not g1.deleted and Path(g1.video_path).exists()
    assert (project.takes_dir / "r.mp4").exists()

    # external file (a seeded Fighter asset) -> NOT moved, just flagged
    ext_dir = Path(tempfile.mkdtemp())
    ext = ext_dir / "BAKE_take.mp4"
    ext.write_bytes(b"external")
    t2 = project.add_take(shot.id, status=STATUS_DONE, video_path=str(ext))
    takes_io.move_to_bin(project.get_take(t2.id), project)
    g2 = project.get_take(t2.id)
    assert g2.deleted and ext.exists() and g2.video_path == str(ext)
    print("takes_io OK: project-owned binned/restored, external file untouched")


def test_takes_view() -> None:
    from PySide6.QtWidgets import QApplication

    from ui.takes_view import TakesView

    app = QApplication.instance() or QApplication([])  # noqa: F841
    tmp = Path(tempfile.mkdtemp())
    project = Project.new()
    shot = project.add_shot("c", model_id="seedance-2.0-std")
    t1, t2 = tmp / "t1.png", tmp / "t2.png"
    _png(t1); _png(t2, (200, 0, 0))
    r1 = project.add_take(shot.id, status=STATUS_DONE, starred=True, thumbnail=str(t1))
    r2 = project.add_take(shot.id, status=STATUS_DONE, thumbnail=str(t2))
    r3 = project.add_take(shot.id, status=STATUS_DONE, thumbnail=str(t2), deleted=True)  # hidden

    tv = TakesView(project, shot.id)
    assert tv.model.rowCount() == 2, tv.model.rowCount()       # r3 hidden
    tv.filter.setCurrentText("Favorites")
    assert tv.model.rowCount() == 1                            # only r1 starred
    tv.toggle_star([r2.id])
    assert tv.model.rowCount() == 2                            # r1 + r2 now starred
    tv.filter.setCurrentText("All")
    tv.delete([r1.id])
    assert tv.model.rowCount() == 1 and project.get_take(r1.id).deleted
    _ = r3

    # Preview height auto-fits the rows the takes actually occupy (1.._MAX_PREVIEW_ROWS),
    # so a single row of takes doesn't reserve an empty second row. Both ends stay pinned
    # (fixed height) so the panel doesn't drift with the window.
    from ui.takes_view import (
        _DRAG_MAX_ROWS, _MAX_PREVIEW_ROWS, columns_for, preview_height, rows_for,
    )
    assert tv.view.minimumHeight() == tv.view.maximumHeight()   # fixed, both ends pinned

    # Pure row/column math (headless-safe, no layout needed).
    assert columns_for(0, 140) == 0                             # width unknown -> can't tell
    assert columns_for(2000, 140) >= 4                          # wide -> several columns
    assert rows_for(2, 2000, 140) == 1                          # 2 takes, wide -> one row
    assert rows_for(50, 2000, 140) == _MAX_PREVIEW_ROWS         # many takes -> capped, scroll
    assert rows_for(0, 2000, 140) == 1                          # empty -> still one row
    assert rows_for(5, 0, 140) == _MAX_PREVIEW_ROWS             # width unknown -> full cap
    assert preview_height(140, 2) > preview_height(140, 1)      # taller for more rows
    assert preview_height(200, 1) > preview_height(140, 1)      # taller for bigger icons

    # A manual drag-resize pins an explicit height (clamped) and overrides auto-fit;
    # clearing it returns to auto-fit.
    s = tv.size_slider.value()
    tv.set_manual_height(99999)                                 # absurd -> clamped to the row cap
    assert tv.view.maximumHeight() == preview_height(s, _DRAG_MAX_ROWS)
    assert tv.view.minimumHeight() == tv.view.maximumHeight()
    tv.set_manual_height(0)                                     # below min -> clamped to one row
    assert tv.view.maximumHeight() == preview_height(s, 1)
    tv.set_manual_height(preview_height(s, 3))                  # in-range -> honored verbatim
    assert tv.view.maximumHeight() == preview_height(s, 3)
    tv.load()                                                   # the manual pin must survive a reload
    assert tv.view.maximumHeight() == preview_height(s, 3)
    tv._apply_icon_size()                                       # ...and an icon-size re-layout
    assert tv.view.maximumHeight() == preview_height(s, 3)
    tv.clear_manual_height()                                    # double-click handle -> back to auto-fit
    assert tv._user_height is None
    print("TakesView OK: filter, star toggle, delete-to-bin, counts, auto-fit + drag-resize height")


def test_take_star_badge() -> None:
    """The clickable star badge: star_badge_rect is the top-left hot-zone, and the delegate's
    editorEvent toggles a take's star (write-through) on a left-click inside it, while a click
    elsewhere in the cell is ignored (left to selection / open)."""
    from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, Qt
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtWidgets import QApplication, QStyleOptionViewItem

    from ui.takes_view import TakesView, star_badge_rect, _STAR_ROLE

    app = QApplication.instance() or QApplication([])  # noqa: F841
    project = Project.new()
    shot = project.add_shot("c", model_id="seedance-2.0-std")
    take = project.add_take(shot.id, status=STATUS_DONE)
    assert not take.starred

    tv = TakesView(project, shot.id)
    assert tv.model.rowCount() == 1
    idx = tv.model.index(0, 0)
    assert idx.data(_STAR_ROLE) is False                       # role seeded unstarred

    cell = QRect(0, 0, 160, 180)
    badge = star_badge_rect(cell)
    assert (badge.left(), badge.top(), badge.width()) == (4, 2, 20)  # top-left inset hot-zone
    opt = QStyleOptionViewItem()
    opt.rect = cell

    def _release(point) -> QMouseEvent:
        p = QPointF(point)
        return QMouseEvent(QEvent.Type.MouseButtonRelease, p, p, Qt.MouseButton.LeftButton,
                           Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)

    # Click inside the badge -> star toggles ON and persists write-through.
    handled = tv._star_delegate.editorEvent(_release(badge.center()), tv.model, opt, idx)
    assert handled is True
    assert project.get_take(take.id).starred, "badge click must star the take"
    # toggle_star reloads the grid in place, so the item's star role reflects the new state
    assert tv.model.index(0, 0).data(_STAR_ROLE) is True, "grid role refreshed after toggle"

    # A click outside the badge is NOT consumed and doesn't change the star.
    outside = QPoint(cell.center().x(), cell.center().y())
    handled2 = tv._star_delegate.editorEvent(_release(outside), tv.model, opt, tv.model.index(0, 0))
    assert handled2 is False
    assert project.get_take(take.id).starred, "click outside the badge must not change the star"
    print("take star badge OK: hot-zone rect, click toggles write-through, miss ignored")


def test_assets_view() -> None:
    from PySide6.QtWidgets import QApplication

    from ui.assets_view import AssetsView

    app = QApplication.instance() or QApplication([])  # noqa: F841
    tmp = Path(tempfile.mkdtemp())
    imgs = []
    for i in range(3):
        q = tmp / f"k{i}.png"; _png(q); imgs.append(str(q))
    project = Project.new()
    av = AssetsView(project)
    assert av.model.rowCount() == 0
    av._import_files(imgs)                          # mimics drag-drop / Import
    assert av.model.rowCount() == 3 and len(project.list_assets()) == 3
    assert all(p.parent == project.assets_dir for p in project.list_assets())
    project.remove_asset(project.list_assets()[0]); av.load()
    assert av.model.rowCount() == 2
    print("AssetsView OK: import (grid + flat in .assets), remove")


def test_asset_picker() -> None:
    from PySide6.QtWidgets import QApplication

    from ui.asset_picker import AssetPickerDialog

    app = QApplication.instance() or QApplication([])  # noqa: F841
    tmp = Path(tempfile.mkdtemp())
    project = Project.new()
    a, b = tmp / "a.png", tmp / "b.png"
    _png(a); _png(b, (0, 0, 200))
    asset_a = str(project.import_asset(a)); project.import_asset(b)

    dlg = AssetPickerDialog(project, current=asset_a)   # no exec() - don't block headless
    assert dlg.model.rowCount() == 2
    assert dlg.selected() == asset_a, "current selection should be pre-highlighted"
    print("AssetPickerDialog OK: grid lists assets, pre-selects current")


def test_card_and_window() -> None:
    from PySide6.QtWidgets import QApplication

    from ui.shot_card import ShotCard
    from ui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])  # noqa: F841
    project = Project.new()
    shot = project.add_shot("kick", model_id="seedance-2.0-std",
                            settings={"seed": 7, "duration": 4, "resolution": "720p"})
    r = project.add_take(shot.id, status=STATUS_DONE, starred=True)

    card = ShotCard(project, shot)
    assert "1 takes" in card.counts.text() and "1★" in card.counts.text()
    card.expand_btn.setChecked(True)
    assert card.takes_view is not None and card.takes_view.model.rowCount() == 1
    card.expand_btn.setChecked(False)
    assert not card.body.isVisible()

    win = MainWindow(project)
    assert len(win.cards) == 1 and shot.id in win.cards
    assert win._card_for_take(r.id) is win.cards[shot.id]
    print("ShotCard + MainWindow OK: counts, expand, card routing")


def test_framed_row_thumbs() -> None:
    from PySide6.QtWidgets import QApplication

    from ui.shot_card import framed_thumb

    app = QApplication.instance() or QApplication([])  # noqa: F841
    tmp = Path(tempfile.mkdtemp())
    # an asset with a real foreground blob (distinct from the corner bg) so keying runs
    img = Image.new("RGB", (64, 64), (0, 200, 0))
    for y in range(20, 50):
        for x in range(20, 50):
            img.putpixel((x, y), (200, 0, 0))
    src = tmp / "kf.png"; img.save(src)

    project = Project.new()
    asset = str(project.import_asset(src))
    shot = project.add_shot("framed", model_id="seedance-2.0-std", start_frame=asset,
                            canvas_w=1254, canvas_h=706,
                            crop={"aspect": "16:9", "start": {"scale": 0.6, "cx": 0.5, "cy": 0.6}})
    short = max(1, round(88 * 706 / 1254))

    pm = framed_thumb(shot, "start", long=88)
    assert not pm.isNull(), "framed start thumb should render"
    assert (pm.width(), pm.height()) == (88, short), "thumb matches the shot's aspect"

    # missing end_frame -> gray placeholder at the same aspect canvas
    ph = framed_thumb(shot, "end", long=88)
    assert not ph.isNull() and (ph.width(), ph.height()) == (88, short)
    print("framed_thumb OK: start renders framed keypose, missing end -> placeholder")


def test_take_progress_label() -> None:
    """A generating take's grid tile shows its live render % (the Queue tab's number),
    fed by the JobManager's progress_pct signal; a late tail after it finishes is ignored."""
    from PySide6.QtWidgets import QApplication

    from backends.jobs import JobManager
    from ui.takes_view import TakesView, progress_percent, take_tile_label

    app = QApplication.instance() or QApplication([])  # noqa: F841

    # Pure label/percent helpers (headless, no widget).
    assert progress_percent(0.5) == "50%"
    assert progress_percent(0.0) == "0%" and progress_percent(1.0) == "100%"
    assert progress_percent(1.4) == "100%" and progress_percent(-0.3) == "0%"   # clamped
    assert take_tile_label("generating", "abc123xyz", "45%").endswith("45%")
    assert "generating" in take_tile_label("generating", "abc123xyz", "")  # no pct -> word
    assert "generating" not in take_tile_label("generating", "abc123xyz", "45%")
    assert take_tile_label("done", "abc123xyz", "") == "abc123"           # bare -> id prefix (star is a badge now)

    project = Project.new()
    shot = project.add_shot("kick", model_id="seedance-2.0-std")
    g = project.add_take(shot.id, status=STATUS_GENERATING)
    jm = JobManager(project)
    tv = TakesView(project, shot.id, jobs=jm)
    assert tv.model.rowCount() == 1
    assert "generating" in tv._items[g.id].text()           # no fraction yet -> bare word

    jm.progress_pct.emit(g.id, 0.5, "step 10/20")           # live render fraction over the signal
    assert "50%" in tv._items[g.id].text()
    assert "generating" not in tv._items[g.id].text()

    # The documented progress_state tail keeps arriving ~20-30s after completion (rule #11):
    # once the take is done it must not be relabelled back to a percentage.
    project.update_take(g.id, status=STATUS_DONE)
    tv.load()
    done_text = tv._items[g.id].text()
    jm.progress_pct.emit(g.id, 0.9, "")              # the emit must be actively ignored...
    assert tv._items[g.id].text() == done_text       # ...the done tile is left exactly as-is
    assert "90%" not in done_text
    print("TakesView progress OK: generating tile shows live %, done tile ignores late tail")


def test_runner_uses_snapshot_not_live_shot() -> None:
    """A queued take renders its frozen settings_snapshot, not the live Shot. Editing the
    source shot's prompt/negative/canvas/crop after queueing (before the serialized worker
    dequeues) must not change what the take renders. Regression for card #53 (rule #3)."""
    from PySide6.QtWidgets import QApplication

    import library
    import pipeline.framing as framing
    from backends import comfy_client, replicate_client
    from ui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])  # noqa: F841

    tmp = Path(tempfile.mkdtemp())
    _png(tmp / "kf.png")

    def queue_and_capture(model_id: str):
        """Queue a take, edit the source shot, then drive the runner _queue_take built with
        the backend's framing/generate stubbed to record what the render actually receives."""
        project = Project.new()
        asset = str(project.import_asset(tmp / "kf.png"))
        shot = project.add_shot(
            "punchy", model_id=model_id, start_frame=asset,
            canvas_w=1254, canvas_h=706,
            crop={"aspect": "16:9", "start": {"scale": 0.6, "cx": 0.5, "cy": 0.4}},
            prompt="punch", negative_prompt="blurry", settings={"seed": 7})
        win = MainWindow(project)
        model = library.get_model(model_id)
        settings = {**model.get("default_params", {}), **shot.settings}

        captured: dict = {}
        win.jobs.enqueue = lambda take_id, backend, runner: captured.update(runner=runner)
        take_id = win._queue_take(shot, model, settings, est=0.0)
        snap = project.get_take(take_id).settings_snapshot

        # Edit the live shot AFTER queueing — the runner must ignore every one of these
        # (settings.length is read by the comfy path off the snapshot's resolved settings).
        project.update_shot(shot.id, prompt="kick", negative_prompt="grainy",
                            canvas_w=512, canvas_h=512,
                            crop={"aspect": "1:1", "start": {"scale": 0.1, "cx": 0.1, "cy": 0.1}},
                            settings={"seed": 7, "length": 99})

        seen: dict = {}
        orig = (framing.render_keyposes, replicate_client.generate,
                comfy_client.generate, comfy_client.ensure_server)
        framing.render_keyposes = lambda s, out_dir: (seen.update(framed=s), (None, None))[1]
        replicate_client.generate = lambda rid, **kw: (
            seen.update(prompt=kw["prompt"], negative=kw["negative"]), "x.mp4")[1]
        comfy_client.generate = lambda tpl, out, **kw: (
            seen.update(prompt=kw["prompt"], negative=kw["negative"], sets=kw["sets"]), {})[1]
        comfy_client.ensure_server = lambda **kw: None
        try:
            captured["runner"](lambda *a, **k: None)   # drive the runner, progress no-op
        finally:
            (framing.render_keyposes, replicate_client.generate,
             comfy_client.generate, comfy_client.ensure_server) = orig
        return snap, seen

    # Hosted (Replicate): prompt/negative via generate, canvas/crop via render_keyposes.
    snap, seen = queue_and_capture("seedance-2.0-std")
    framed = seen["framed"]
    assert seen["prompt"] == snap["prompt"] == "punch"            # frozen, not "kick"
    assert seen["negative"] == snap["negative_prompt"] == "blurry"
    assert (framed.canvas_w, framed.canvas_h) == tuple(snap["canvas"]) == (1254, 706)
    assert framed.crop == snap["crop"] and framed.crop["aspect"] == "16:9"   # not "1:1"
    assert framed.start_frame == snap["start_frame"]

    # Local (ComfyUI): same fields through the crash-recovery-wrapped attempt(); size_sets
    # is derived from the snapshot's frozen canvas + resolved length, not the edited live
    # shot (512x512, length 99). length comes from default_params (17), proving it's read
    # off the snapshot's resolved settings, not the live shot's raw settings.
    snap, seen = queue_and_capture("local-flf-wan14b")
    framed = seen["framed"]
    assert seen["prompt"] == snap["prompt"] == "punch"
    assert seen["negative"] == snap["negative_prompt"] == "blurry"
    assert (framed.canvas_w, framed.canvas_h) == tuple(snap["canvas"]) == (1254, 706)
    assert framed.crop == snap["crop"] and framed.crop["aspect"] == "16:9"
    sizes = set(seen["sets"].values())
    assert 1254 in sizes and 706 in sizes and 512 not in sizes
    assert snap["settings"]["length"] == 17 and 17 in sizes and 99 not in sizes
    print("runner snapshot freeze OK: both backends render the frozen snapshot, not the edited shot")


def test_snapshot_detached_from_live_shot_at_creation() -> None:
    """_queue_take freezes the take's settings_snapshot DETACHED from the live shot: an
    in-place mutation of shot.crop (or a nested settings dict the caller still holds) after
    queueing must NOT reach the immutable snapshot. Creation-side mirror of card #53 / the
    render-side detach in _shot_from_snapshot (rule #3)."""
    from PySide6.QtWidgets import QApplication

    import library
    from ui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])  # noqa: F841

    tmp = Path(tempfile.mkdtemp())
    _png(tmp / "kf.png")
    project = Project.new()
    asset = str(project.import_asset(tmp / "kf.png"))
    shot = project.add_shot(
        "punchy", model_id="seedance-2.0-std", start_frame=asset,
        canvas_w=1254, canvas_h=706,
        crop={"aspect": "16:9", "start": {"scale": 0.6, "cx": 0.5, "cy": 0.4}},
        prompt="punch", negative_prompt="blurry", settings={"seed": 7})
    win = MainWindow(project)
    model = library.get_model("seedance-2.0-std")
    # A nested dict in settings is the aliasing vector the shallow dict() copy can't cover.
    settings = {**model.get("default_params", {}), **shot.settings, "extra": {"k": 1}}

    win.jobs.enqueue = lambda *a, **k: None          # freeze the snapshot, run nothing
    take_id = win._queue_take(shot, model, settings, est=0.0)
    snap = project.get_take(take_id).settings_snapshot
    assert snap["crop"]["start"]["scale"] == 0.6     # captured at creation
    assert snap["settings"]["extra"]["k"] == 1

    # Mutate the live shot's crop IN PLACE (not update_shot replacing the whole dict) and the
    # nested settings dict the caller still references — the frozen snapshot must not move.
    shot.crop["start"]["scale"] = 999
    shot.crop["aspect"] = "1:1"
    settings["extra"]["k"] = 999
    assert snap["crop"]["start"]["scale"] == 0.6, "in-place crop mutation leaked into the snapshot"
    assert snap["crop"]["aspect"] == "16:9"
    assert snap["settings"]["extra"]["k"] == 1, "nested settings mutation leaked into the snapshot"
    print("snapshot creation-detach OK: in-place crop/settings mutation can't corrupt the frozen snapshot")


def test_param_enum_preserves_out_of_schema_value() -> None:
    """The generic enum-param combo must PRESERVE a stored value absent from the live schema
    enum (e.g. an option renamed/removed by a Model Library refresh) instead of silently
    snapping to enum[0] and overwriting the user's choice. The stale value is kept selectable
    and flagged red (mirrors the invalid-aspect handling) so the user re-picks deliberately."""
    from PySide6.QtWidgets import QApplication, QComboBox

    from ui.shot_tab import ShotTab

    app = QApplication.instance() or QApplication([])  # noqa: F841
    project = Project.new()
    shot = project.add_shot("c", model_id="seedance-2.0-std")
    tab = ShotTab(project, shot)
    model = tab._current_model() or {}

    # A live schema whose enum no longer contains the stored value 'pro'.
    tab._schema = {"quality": {"enum": ["standard", "high"]}}

    # In-enum value: selected normally, no red flag.
    w_ok, get_ok = tab._make_param_widget("quality", "high", model)
    assert isinstance(w_ok, QComboBox)
    assert get_ok() == "high"
    assert "d9534f" not in w_ok.styleSheet()                  # valid -> no red border

    # Out-of-enum value: preserved (NOT enum[0]='standard') and flagged red to re-pick.
    w_bad, get_bad = tab._make_param_widget("quality", "pro", model)
    assert isinstance(w_bad, QComboBox)
    assert get_bad() == "pro", "stored value must be preserved, not snapped to enum[0]"
    assert get_bad() != "standard"
    assert "pro" in [w_bad.itemText(i) for i in range(w_bad.count())]  # kept as a selectable item
    assert "d9534f" in w_bad.styleSheet()                     # flagged red like an invalid aspect

    # Re-picking a valid option clears the flag.
    w_bad.setCurrentText("standard")
    assert get_bad() == "standard"
    assert "d9534f" not in w_bad.styleSheet()
    print("shot-tab enum param OK: out-of-schema value preserved + flagged, valid re-pick clears flag")


def test_takes_view_incremental_update() -> None:
    """A take's status signal updates just that take's tile in place (same QStandardItem, cached
    icon, no full model rebuild), and only falls back to a full load when the take's membership
    in the current view actually changed - card #75."""
    from PySide6.QtWidgets import QApplication

    from ui.takes_view import TakesView, _STAR_ROLE

    app = QApplication.instance() or QApplication([])  # noqa: F841

    project = Project.new()
    shot = project.add_shot("kick", model_id="seedance-2.0-std")
    t1 = project.add_take(shot.id, status=STATUS_PENDING)
    t2 = project.add_take(shot.id, status=STATUS_GENERATING)
    tv = TakesView(project, shot.id)
    assert tv.model.rowCount() == 2

    # The QIcon cache hands back the SAME object for an unchanged take (no disk re-decode) ...
    td = project.add_take(shot.id, status=STATUS_PENDING)
    tv.load()
    icon_pending = tv._icon_for(project.get_take(td.id))
    assert tv._icon_for(project.get_take(td.id)) is icon_pending
    # ... but INVALIDATES when the content signature changes (here the status placeholder), so a
    # frozen cache key (e.g. dropping the mtime/status from it) would be caught.
    project.update_take(td.id, status=STATUS_FAILED)
    assert tv._icon_for(project.get_take(td.id)) is not icon_pending

    # A status transition updates the existing item IN PLACE - same object, no model.clear().
    item1 = tv._items[t1.id]
    project.update_take(t1.id, status=STATUS_GENERATING)
    tv.update_take(t1.id)
    assert tv._items[t1.id] is item1                 # not rebuilt
    assert "generating" in tv._items[t1.id].text()    # label reflects the new status
    assert tv.model.rowCount() == 3                   # membership unchanged - no reload

    # A star flip is reflected on the existing item's star role, still no reload.
    item2 = tv._items[t2.id]
    project.set_starred(t2.id, True)
    tv.update_take(t2.id)
    assert tv._items[t2.id] is item2
    assert tv._items[t2.id].data(_STAR_ROLE) is True

    # A take not yet in the grid (added after the last load) falls back to a full load so it shows.
    t4 = project.add_take(shot.id, status=STATUS_PENDING)
    tv.update_take(t4.id)
    assert tv.model.rowCount() == 4 and t4.id in tv._items

    # Under the Favorites filter, a status change to a NON-starred take is a correct no-op: it
    # isn't shown and shouldn't be, so no spurious reload (which would reset running animations).
    tv.filter.setCurrentText("Favorites")            # load(): only t2 is starred
    assert tv.model.rowCount() == 1 and t2.id in tv._items
    shown = tv._items[t2.id]
    project.update_take(t1.id, status=STATUS_DONE)    # t1 isn't starred -> not in this view
    tv.update_take(t1.id)
    assert tv.model.rowCount() == 1                   # no new row, no reload
    assert tv._items[t2.id] is shown                  # the visible starred take is untouched

    # The inverse boundary cross: a SHOWN starred take that becomes unstarred under Favorites must
    # fall back to load() and drop from the view (the most regression-prone branch).
    project.set_starred(t2.id, False)
    tv.update_take(t2.id)
    assert tv.model.rowCount() == 0 and t2.id not in tv._items
    print("TakesView incremental OK: in-place tile update, icon cache, membership fallback")


def test_take_star_toggle_incremental() -> None:
    """toggle_star (badge click + right-click 'Toggle star') updates the starred take's tile IN
    PLACE - same QStandardItem, no model.clear()+rebuild + every-take PyAV strip re-decode - which
    was the seconds-long UI freeze. Only the one genuine membership cross (un-starring a shown take
    under the Favorites filter) falls back to a full load()."""
    from PySide6.QtWidgets import QApplication

    from ui.takes_view import TakesView, _STAR_ROLE

    app = QApplication.instance() or QApplication([])  # noqa: F841

    project = Project.new()
    shot = project.add_shot("kick", model_id="seedance-2.0-std")
    t1 = project.add_take(shot.id, status=STATUS_DONE)
    t2 = project.add_take(shot.id, status=STATUS_DONE)
    tv = TakesView(project, shot.id)
    assert tv.model.rowCount() == 2

    # Star a take via toggle_star: the EXISTING item object survives (a load() would replace it),
    # the star role flips, and the row count is unchanged (no rebuild).
    item1 = tv._items[t1.id]
    tv.toggle_star([t1.id])
    assert tv._items[t1.id] is item1, "star toggle must not rebuild the model"
    assert tv._items[t1.id].data(_STAR_ROLE) is True
    assert project.get_take(t1.id).starred is True       # write-through persisted
    assert tv.model.rowCount() == 2

    # Un-star it again (still no membership change outside Favorites) - same in-place path.
    tv.toggle_star([t1.id])
    assert tv._items[t1.id] is item1
    assert tv._items[t1.id].data(_STAR_ROLE) is False

    # The badge-click entry point (_toggle_star_by_id) routes through the same in-place path.
    item2 = tv._items[t2.id]
    tv._toggle_star_by_id(t2.id)
    assert tv._items[t2.id] is item2 and tv._items[t2.id].data(_STAR_ROLE) is True

    # The `is item1`/`is item2` identity checks above are the regression guard for the optimization
    # itself: a revert to the old unconditional load() rebuilds _items with fresh QStandardItems and
    # would fail them. This last case instead guards the one correct fallback - un-starring a SHOWN
    # starred take under the Favorites filter must drop it from the view (membership cross -> load()).
    tv.filter.setCurrentText("Favorites")
    assert tv.model.rowCount() == 1 and t2.id in tv._items
    tv.toggle_star([t2.id])
    assert tv.model.rowCount() == 0 and t2.id not in tv._items
    print("TakesView star toggle OK: in-place tile update, no rebuild, Favorites membership fallback")


def test_queue_view() -> None:
    """The Queue tab is a model + delegate with ZERO per-row widgets (rule #18 root cause):
    the child-widget count stays bounded no matter how deep the pending queue is, a progress
    tick repaints just one row's Progress cell, and a burst of status signals (the mass-cancel
    storm that overflowed paintSiblingsRecursive) coalesces to a single rebuild - not one per
    signal."""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QProgressBar, QPushButton, QWidget

    from backends.jobs import JobManager
    from store.models import STATUS_CANCELLED, STATUS_DONE, STATUS_GENERATING, STATUS_PENDING
    from ui.queue_view import QueueView, _BAR_LABEL_ROLE, _BAR_ROLE, _PROGRESS_COL

    app = QApplication.instance() or QApplication([])  # noqa: F841

    project = Project.new()
    shot = project.add_shot("kick", model_id="seedance-2.0-std")
    jobs = JobManager(project)

    def add(status, backend="replicate", model_id="seedance-2.0-std"):
        return project.add_take(shot.id, status=status,
                                settings_snapshot={"model_id": model_id, "backend": backend})

    # Shallow queue first, to baseline the child-widget count.
    for _ in range(3):
        add(STATUS_PENDING)
    qv = QueueView(project, jobs)
    qv.refresh()
    assert qv.findChildren(QProgressBar) == []                 # no per-row progress bars
    assert qv.table.findChildren(QPushButton) == []            # no per-row Cancel buttons
    shallow_children = len(qv.findChildren(QWidget))

    # (a) Widget count does NOT scale with pending depth: add 200 more pending takes.
    for _ in range(200):
        add(STATUS_PENDING)
    qv.refresh()
    assert qv.model.rowCount() == 203                           # all active shown (never capped)
    assert qv.findChildren(QProgressBar) == []
    assert len(qv.findChildren(QWidget)) == shallow_children, "child count must stay bounded"

    # (b) A progress tick on a still-rendering local take touches exactly one row's Progress cell.
    g = add(STATUS_GENERATING, backend="comfyui", model_id="local-flf-wan14b")
    qv.refresh()
    # before any tick a local generating take already shows a determinate 0% bar (not text).
    g_idx = qv.model.index(next(i for i, t in enumerate(qv.model.rows()) if t.id == g.id),
                           _PROGRESS_COL)
    assert qv.model.data(g_idx, _BAR_ROLE) == 0 and qv.model.data(g_idx, _BAR_LABEL_ROLE) == "0%"
    touched: list = []
    qv.model.dataChanged.connect(
        lambda tl, br, *_: touched.append((tl.row(), tl.column(), br.row(), br.column())))
    jobs.progress_pct.emit(g.id, 0.5, "step 1/2")
    assert len(touched) == 1, touched
    row, col, br_row, br_col = touched[0]
    assert (row, col) == (br_row, br_col) == (row, _PROGRESS_COL)   # one cell, one row
    # the model now feeds the delegate a determinate 50% bar + the WS step label for that take
    assert qv.model.data(qv.model.index(row, _PROGRESS_COL), _BAR_ROLE) == 50
    assert qv.model.data(qv.model.index(row, _PROGRESS_COL), _BAR_LABEL_ROLE) == "step 1/2"
    # a hosted/queued take is plain text, no bar
    pend_row = next(i for i, t in enumerate(qv.model.rows()) if t.status == STATUS_PENDING)
    assert qv.model.data(qv.model.index(pend_row, _PROGRESS_COL), _BAR_ROLE) is None
    assert qv.model.data(qv.model.index(pend_row, _PROGRESS_COL),
                         Qt.ItemDataRole.DisplayRole) == "queued"

    # (c) Coalescing: emitting status_changed per take (like cancel_pending / abandon_local)
    #     arms the timer once; the actual rebuild fires a SINGLE time on the next loop turn.
    before = qv._rebuild_count
    for t in project.list_takes():
        jobs.status_changed.emit(t.id, STATUS_CANCELLED)
    assert qv._rebuild_count == before                         # nothing rebuilt yet - just armed
    app.processEvents()                                        # fire the coalescing timer
    assert qv._rebuild_count == before + 1, qv._rebuild_count  # one rebuild, not 204

    # (d) The queued-take right-click menu (no exec()) only offers Cancel for PENDING takes.
    pending_id = next(t.id for t in qv.model.rows() if t.status == STATUS_PENDING)
    menu = qv._build_context_menu([pending_id])
    assert [a.text() for a in menu.actions()] == ["Cancel queued generation"]
    assert qv._build_context_menu([g.id]).actions() == []      # a generating take: nothing to cancel

    # (d2) Header gains a cumulative 'N done' counter and the 'Last finished' strip surfaces the
    #      newest finished take, so a result is visible without scrolling the queue (card #77).
    #      (isHidden(), not isVisible(): the unshown headless widget is never "visible" but its
    #      explicit show/hide intent is what we drive.)
    assert qv.last_label.isHidden()                            # nothing finished yet -> strip hidden
    d = add(STATUS_DONE)
    project.update_take(d.id, started="2026-06-18T10:00:00", completed="2026-06-18T10:00:12")
    qv.refresh()
    assert "1 done" in qv.summary.text(), qv.summary.text()
    assert not qv.last_label.isHidden()                        # a finished take -> strip shown
    assert qv.last_label.text() == "Last finished: kick - done in 12s", qv.last_label.text()

    # (e) Force a real paint so _ProgressDelegate.paint actually runs (the determinate-bar
    #     branch for the local take + the plain-text branch for the rest) - the model-role
    #     asserts above don't exercise the paint path.
    qv.resize(900, 360)
    assert not qv.grab().isNull()
    print("QueueView OK: zero per-row widgets, bounded child count, 1-row progress, coalesced rebuild, cancel menu, done counter, last-finished strip, paint")


if __name__ == "__main__":
    test_bin_restore()
    test_takes_view()
    test_takes_view_incremental_update()
    test_take_star_badge()
    test_take_star_toggle_incremental()
    test_take_progress_label()
    test_assets_view()
    test_asset_picker()
    test_card_and_window()
    test_framed_row_thumbs()
    test_runner_uses_snapshot_not_live_shot()
    test_snapshot_detached_from_live_shot_at_creation()
    test_param_enum_preserves_out_of_schema_value()
    test_queue_view()
    print("PHASE 4 SMOKE: PASS")
