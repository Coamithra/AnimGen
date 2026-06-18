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
from store.models import STATUS_DONE, STATUS_GENERATING  # noqa: E402


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


if __name__ == "__main__":
    test_bin_restore()
    test_takes_view()
    test_take_star_badge()
    test_take_progress_label()
    test_assets_view()
    test_asset_picker()
    test_card_and_window()
    test_framed_row_thumbs()
    print("PHASE 4 SMOKE: PASS")
