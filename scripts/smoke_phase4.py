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
    STATUS_CANCELLED, STATUS_DONE, STATUS_FAILED, STATUS_GENERATING, STATUS_PENDING,
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


def test_move_to_bin_partial_failure() -> None:
    """M13: move_to_bin is per-move atomic. A transient AV/indexer lock on a later file move
    (the thumbnail here) must NOT strand the record - the already-moved video keeps its .bin
    path, the take is flagged deleted, and restore_from_bin still puts the moved file back
    while leaving the never-moved one where it is. The caller loop is also resilient: one
    failing take doesn't abort binning the rest of a multi-delete."""
    import shutil as _shutil

    from pipeline import takes_io

    project = Project.new()
    shot = project.add_shot("c", model_id="seedance-2.0-std")

    video = project.takes_dir / "v.mp4"
    video.write_bytes(b"video")
    thumb = project.thumbs_dir / "v.png"
    _png(thumb)
    t = project.add_take(shot.id, status=STATUS_DONE,
                         video_path=str(video), thumbnail=str(thumb))

    # Fail the SECOND real move (the thumbnail) mid-sequence - the documented Windows AV-lock
    # failure mode. The video (moved first) must already be recorded + the take flagged deleted.
    real_move = _shutil.move
    orig_thumb = str(thumb)

    def flaky_move(src, dst):
        if str(src) == orig_thumb:
            raise OSError("simulated AV lock on thumbnail move")
        return real_move(src, dst)

    _shutil.move = flaky_move
    try:
        try:
            takes_io.move_to_bin(project.get_take(t.id), project)
        except OSError:
            pass  # move_to_bin lets the failing move propagate; caller (below) swallows it
    finally:
        _shutil.move = real_move

    g = project.get_take(t.id)
    # Record consistent with disk: video binned + recorded, deleted set; thumbnail untouched.
    assert g.deleted, "take must be flagged deleted even though a later move failed"
    assert not video.exists(), "video should have moved to .bin"
    assert (project.bin_dir / t.id / "v.mp4").exists()
    assert Path(g.video_path).exists() and g.video_path == str(project.bin_dir / t.id / "v.mp4")
    assert g.thumbnail == orig_thumb and thumb.exists(), "failed thumbnail move stays in place"

    # Restore a PARTIALLY-binned take: the video (under .bin) moves back, the thumbnail (never
    # under .bin) is skipped and left exactly where it is. Both symmetric with the good path.
    takes_io.restore_from_bin(project.get_take(t.id), project)
    g = project.get_take(t.id)
    assert not g.deleted
    assert (project.takes_dir / "v.mp4").exists() and Path(g.video_path).exists()
    assert g.thumbnail == orig_thumb and thumb.exists()

    # Restore is per-move atomic in mirror (M13): bin a take fully, then fail the thumbnail
    # move-BACK mid-restore. The video (restored first) keeps its recorded takes/ path and
    # deleted flips False; the thumbnail stays in .bin with its record intact, so a retried
    # restore (no failure) completes it.
    v2 = project.takes_dir / "w.mp4"
    v2.write_bytes(b"video2")
    th2 = project.thumbs_dir / "w.png"
    _png(th2)
    t2 = project.add_take(shot.id, status=STATUS_DONE,
                          video_path=str(v2), thumbnail=str(th2))
    takes_io.move_to_bin(project.get_take(t2.id), project)         # full bin, no failure
    binned_thumb = project.get_take(t2.id).thumbnail

    def flaky_restore(src, dst):
        if str(src) == binned_thumb:
            raise OSError("simulated AV lock on thumbnail move-back")
        return real_move(src, dst)

    _shutil.move = flaky_restore
    try:
        try:
            takes_io.restore_from_bin(project.get_take(t2.id), project)
        except OSError:
            pass
    finally:
        _shutil.move = real_move
    g2 = project.get_take(t2.id)
    assert not g2.deleted
    assert g2.video_path == str(project.takes_dir / "w.mp4") and Path(g2.video_path).exists()
    assert g2.thumbnail == binned_thumb and Path(binned_thumb).exists()
    takes_io.restore_from_bin(project.get_take(t2.id), project)    # retry completes it
    g2 = project.get_take(t2.id)
    assert g2.thumbnail == str(project.thumbs_dir / "w.png") and Path(g2.thumbnail).exists()

    # Caller resilience: TakesView.delete over multiple ids - one take fails its file move but
    # the loop still bins the rest.
    from PySide6.QtWidgets import QApplication

    from ui.takes_view import TakesView

    app = QApplication.instance() or QApplication([])  # noqa: F841
    va = project.takes_dir / "a.mp4"; va.write_bytes(b"a")
    vb = project.takes_dir / "b.mp4"; vb.write_bytes(b"b")
    ta = project.add_take(shot.id, status=STATUS_DONE, video_path=str(va))
    tb = project.add_take(shot.id, status=STATUS_DONE, video_path=str(vb))
    bad = str(va)

    def flaky_move2(src, dst):
        if str(src) == bad:
            raise OSError("simulated AV lock")
        return real_move(src, dst)

    _shutil.move = flaky_move2
    try:
        TakesView(project, shot.id).delete([ta.id, tb.id])
    finally:
        _shutil.move = real_move
    # ta failed its move but is still flagged deleted (per-move: deleted set before the move);
    # tb bins fully despite ta's failure - the loop wasn't aborted.
    assert project.get_take(ta.id).deleted
    gb = project.get_take(tb.id)
    assert gb.deleted and not vb.exists() and (project.bin_dir / tb.id / "b.mp4").exists()
    print("takes_io OK: per-move atomic on failure; restore of partial bin; caller loop resilient")


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


def test_replace_background_ui() -> None:
    import numpy as np

    from PySide6.QtWidgets import QApplication

    from pipeline import bg_replace
    from ui.assets_view import AssetsView
    from ui.bg_replace_dialog import BackgroundReplaceDialog

    app = QApplication.instance() or QApplication([])  # noqa: F841
    tmp = Path(tempfile.mkdtemp())
    arr = np.zeros((64, 64, 3), np.uint8)
    arr[:] = (0, 255, 0)                 # green screen
    arr[16:48, 16:48] = (200, 120, 60)   # opaque character block
    src = tmp / "greenscreen.png"; Image.fromarray(arr, "RGB").save(src)

    project = Project.new()
    av = AssetsView(project)
    av._import_files([str(src)])
    asset = str(project.list_assets()[0])

    # dialog prefills the source from the corner sample; default fill is the contract magenta
    prefill = bg_replace.nearest_chroma(bg_replace.sample_corner(Image.open(asset))) or bg_replace.AUTO
    dlg = BackgroundReplaceDialog(prefill)          # no exec() - headless
    assert dlg.source() == "Green" and dlg.fill_rgb() == (255, 0, 255)

    # context menu offers Replace background… (built without exec)
    _menu, acts = av._build_context_menu([asset])
    assert set(acts) == {"replace_bg", "delete"}

    av._apply_replace_background([asset], dlg.source(), dlg.fill_rgb())
    corner = tuple(int(x) for x in np.array(Image.open(asset).convert("RGB"))[0, 0])
    assert corner == (255, 0, 255), corner
    meta = project.asset_meta(asset)
    assert meta["source_chroma"] == "Green" and project.transparent_ref(asset) is not None

    # a re-fill REUSES the stored transparent ref instead of re-keying our added magenta:
    # spy on key_to_transparent - it must not be called on the reuse path
    calls = []
    orig = bg_replace.key_to_transparent
    bg_replace.key_to_transparent = lambda *a, **k: (calls.append(1), orig(*a, **k))[1]
    try:
        av._apply_replace_background([asset], "Green", (0, 0, 255))
    finally:
        bg_replace.key_to_transparent = orig
    assert calls == [], "re-fill must reuse the stored transparent sprite, not re-key"
    corner2 = tuple(int(x) for x in np.array(Image.open(asset).convert("RGB"))[0, 0])
    assert corner2 == (0, 0, 255), corner2
    print("Replace background OK: prefill, key+fill, re-fill reuses transparent ref (no re-key)")


def test_transparent_import_forces_bg() -> None:
    import numpy as np

    from PySide6.QtWidgets import QApplication

    from pipeline import bg_replace
    from ui.assets_view import AssetsView

    app = QApplication.instance() or QApplication([])  # noqa: F841
    tmp = Path(tempfile.mkdtemp())
    ta = np.zeros((32, 32, 4), np.uint8)
    ta[8:24, 8:24] = (30, 60, 90, 255)   # opaque char on a transparent background
    src = tmp / "sprite.png"; Image.fromarray(ta, "RGBA").save(src)

    project = Project.new()
    av = AssetsView(project)
    av._import_files([str(src)])
    asset = next(p for p in project.list_assets() if p.name == "sprite.png")

    im = Image.open(asset)
    assert not bg_replace.has_transparency(im), "transparent import must be flattened opaque"
    corner = tuple(int(x) for x in np.array(im.convert("RGB"))[0, 0])
    assert corner == (255, 0, 255), corner
    meta = project.asset_meta(asset)
    assert meta.get("imported_transparent") is True and project.transparent_ref(asset) is not None

    # a fully-opaque import is left untouched (no forced composite, no stored ref)
    flat = tmp / "flat.png"; Image.new("RGB", (16, 16), (5, 6, 7)).save(flat)
    av._import_files([str(flat)])
    fasset = next(p for p in project.list_assets() if p.name == "flat.png")
    assert "transparent_ref" not in project.asset_meta(fasset)
    print("Transparent import OK: alpha flattened onto magenta + ref stored; opaque left as-is")


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


def test_take_tile_tooltip() -> None:
    """A take tile's rich hover tooltip surfaces the per-take metadata the tile can't (status,
    when queued, seed, render duration, cost, model/backend) and the full error text for a
    FAILED take, so a failed tile reveals *why* without opening the viewer (card UX7). The
    helper is pure (Take in -> string out): no disk/PyAV/library access, so it stays cheap to
    build per tile on the GUI thread."""
    from PySide6.QtWidgets import QApplication

    from store.models import Take
    from ui.takes_view import TakesView, take_tile_tooltip, _render_duration

    # Pure duration helper (headless): started -> completed, "" on missing / unparseable / skew.
    assert _render_duration("2026-06-18T10:00:00", "2026-06-18T10:00:12") == "12s"
    assert _render_duration("2026-06-18T10:00:00", "2026-06-18T10:03:15") == "3m15s"
    assert _render_duration("2026-06-18T10:00:00", "2026-06-18T11:02:03") == "1h2m3s"
    assert _render_duration("", "2026-06-18T10:00:12") == ""          # missing start
    assert _render_duration("2026-06-18T10:00:12", "2026-06-18T10:00:00") == ""  # negative -> ""
    assert _render_duration("garbage", "2026-06-18T10:00:12") == ""   # unparseable -> ""

    assert take_tile_tooltip(None) == ""

    # A done take: status, model+backend, created, render time, seed, and (actual) cost.
    done = Take(
        id="d1", shot_id="s", status=STATUS_DONE,
        settings_snapshot={"model_id": "seedance-2.0-std", "backend": "replicate",
                           "settings": {"seed": 777}},
        seed=1234, cost_estimate=0.05, cost_actual=0.042,
        created="2026-06-18T10:00:00", started="2026-06-18T10:00:03",
        completed="2026-06-18T10:00:15",
    )
    tip = take_tile_tooltip(done)
    assert "Status: done" in tip
    assert "Model: seedance-2.0-std (replicate)" in tip
    assert "Created: 2026-06-18T10:00:00" in tip
    assert "Render time: 12s" in tip                     # started -> completed, not created
    assert "Seed: 1234" in tip                            # take.seed (authoritative post-roll)
    assert "Cost: $0.042" in tip and "Est. cost" not in tip   # actual present -> actual shown

    # A failed take: the FULL error text is in the tooltip so the tile reveals why.
    failed = Take(
        id="f1", shot_id="s", status=STATUS_FAILED,
        error="CUDA out of memory: tried to allocate 2.5 GiB",
    )
    ftip = take_tile_tooltip(failed)
    assert "Status: failed" in ftip
    assert "Error:" in ftip
    assert "CUDA out of memory: tried to allocate 2.5 GiB" in ftip

    # A failed take with no recorded error still gets an explicit note (not a bare "Error:").
    assert "(no error detail was recorded)" in take_tile_tooltip(
        Take(id="f2", shot_id="s", status=STATUS_FAILED))

    # A crash-interrupted FAILED take is restartable (rule #17) - the error label says so, so a
    # hover distinguishes a crash victim from a genuine workflow failure.
    itip = take_tile_tooltip(
        Take(id="f3", shot_id="s", status=STATUS_FAILED, interrupted=True, error="lost render"))
    assert "restartable" in itip and "lost render" in itip
    assert "restartable" not in ftip                     # a genuine failure is NOT labelled restartable

    # Est. cost is labelled as an estimate when no actual is recorded; a not-yet-rolled take
    # shows no seed (the snapshot's pre-roll placeholder is deliberately not surfaced) and no
    # render-time line (not finished).
    pend = Take(id="p1", shot_id="s", status=STATUS_PENDING, cost_estimate=0.08,
                settings_snapshot={"settings": {"seed": 999}})
    ptip = take_tile_tooltip(pend)
    assert "Est. cost: $0.080" in ptip
    assert "Seed" not in ptip                             # no post-roll seed yet -> no seed line
    assert "Render time" not in ptip                     # not finished -> no duration line

    # Wired live: the tooltip is set on the item in load() (full metadata) and refreshed in
    # update_take() on a status transition - both go through the real widget, not just the helper.
    app = QApplication.instance() or QApplication([])  # noqa: F841
    project = Project.new()
    shot = project.add_shot("kick", model_id="seedance-2.0-std")
    d = project.add_take(
        shot.id, status=STATUS_DONE, seed=1234, cost_estimate=0.05,
        settings_snapshot={"model_id": "seedance-2.0-std", "backend": "replicate"},
        started="2026-06-18T10:00:00", completed="2026-06-18T10:00:12")
    tv = TakesView(project, shot.id)
    dtip = tv._items[d.id].toolTip()                      # what actually landed on the item via load()
    assert "Status: done" in dtip
    assert "Model: seedance-2.0-std (replicate)" in dtip
    assert "Render time: 12s" in dtip and "Seed: 1234" in dtip and "Est. cost: $0.050" in dtip
    t = project.add_take(shot.id, status=STATUS_PENDING, cost_estimate=0.05)
    tv.load()
    assert "Status: pending" in tv._items[t.id].toolTip()
    project.update_take(t.id, status=STATUS_FAILED, error="boom")
    tv.update_take(t.id)
    assert "Status: failed" in tv._items[t.id].toolTip()
    assert "boom" in tv._items[t.id].toolTip()           # error refreshed on the transition
    print("TakesView tooltip OK: rich per-take metadata, full error + interrupted note, live on load/update")


def test_bin_neutralizes_queued_take() -> None:
    """Deleting a non-terminal take to the bin must first neutralize it in the queue (H2):
    a PENDING take is cancelled (so its runnable never fires the backend), a GENERATING one
    is asked to stop (so spend/GPU halts). A bare move_to_bin only sets deleted=True and the
    queue scans (include_deleted=False) would then never touch it again."""
    from PySide6.QtWidgets import QApplication

    from backends.jobs import JobManager
    from store.models import STATUS_CANCELLED
    from ui.takes_view import TakesView

    app = QApplication.instance() or QApplication([])  # noqa: F841

    project = Project.new()
    shot = project.add_shot("kick", model_id="seedance-2.0-std")
    jm = JobManager(project)

    # Bin-while-PENDING: cancel_take flips it to CANCELLED and records it in _cancelled, then
    # it's binned. Without the neutralize step it would stay PENDING+deleted and its runnable
    # would later fire the backend into a binned take.
    p = project.add_take(shot.id, status=STATUS_PENDING,
                         settings_snapshot={"backend": "comfyui"})
    tv = TakesView(project, shot.id, jobs=jm)
    tv.delete([p.id])
    got = project.get_take(p.id)
    assert got.deleted and got.status == STATUS_CANCELLED, (got.deleted, got.status)
    assert p.id in jm._cancelled

    # Bin-while-GENERATING: request_stop flags it in _stopping (the worker would unwind to
    # CANCELLED) and issues the best-effort backend stop; using replicate with no backend_job_id
    # keeps the backend cancel a no-op (no network). Then it's binned.
    g = project.add_take(shot.id, status=STATUS_GENERATING,
                         settings_snapshot={"backend": "replicate"})
    tv.load()
    tv.delete([g.id])
    assert g.id in jm._stopping
    assert project.get_take(g.id).deleted

    # Belt-and-braces: the queue-wide scans now sweep a binned-but-PENDING take too, so even a
    # bin that somehow skipped the neutralize step can't leave a runnable firing / a stuck take.
    project2 = Project.new()
    shot2 = project2.add_shot("punch", model_id="local-flf-wan14b")
    jm2 = JobManager(project2)
    binned = project2.add_take(shot2.id, status=STATUS_PENDING, deleted=True,
                               settings_snapshot={"backend": "comfyui"})
    assert jm2.cancel_pending() == 1                       # the binned pending take is swept
    assert project2.get_take(binned.id).status == STATUS_CANCELLED

    binned2 = project2.add_take(shot2.id, status=STATUS_PENDING, deleted=True,
                                settings_snapshot={"backend": "comfyui"})
    held = jm2.pause_local()                               # ... and held on pause, not dropped
    assert binned2.id in held
    jm2.clear_local_pause()

    binned3 = project2.add_take(shot2.id, status=STATUS_PENDING, deleted=True,
                                settings_snapshot={"backend": "comfyui"})
    # binned2 is still PENDING (held by the pause above, never resumed), so abandon_local
    # sweeps exactly binned2 + binned3 - a never-resumed held binned take can't stick either.
    assert jm2.abandon_local("crashed") == 2
    assert project2.get_take(binned2.id).status == STATUS_CANCELLED
    assert project2.get_take(binned3.id).status == STATUS_CANCELLED
    print("TakesView bin OK: pending cancelled + generating stopped on bin; scans sweep binned")


def test_takes_view_stop_rendering_menu() -> None:
    """The takes-grid right-click menu (no exec()) offers 'Stop rendering' for a GENERATING take
    when a JobManager is wired in, routing to request_stop so spend/GPU halts in place (UX1). A
    non-generating take (or a view with no jobs) offers no such entry."""
    from PySide6.QtWidgets import QApplication

    from backends.jobs import JobManager
    from ui.takes_view import TakesView

    app = QApplication.instance() or QApplication([])  # noqa: F841

    project = Project.new()
    shot = project.add_shot("kick", model_id="seedance-2.0-std")
    jm = JobManager(project)
    g = project.add_take(shot.id, status=STATUS_GENERATING,
                         settings_snapshot={"backend": "replicate"})
    d = project.add_take(shot.id, status=STATUS_DONE)

    tv = TakesView(project, shot.id, jobs=jm)
    # A GENERATING take offers Stop rendering.
    assert "Stop rendering" in [a.text() for a in tv._build_context_menu([g.id]).actions()]
    # A DONE take does not.
    assert "Stop rendering" not in [a.text() for a in tv._build_context_menu([d.id]).actions()]
    # A mixed selection offers it (acting only on the generating take).
    labels = [a.text() for a in tv._build_context_menu([g.id, d.id]).actions()]
    assert "Stop rendering" in labels

    # Firing it routes to JobManager.request_stop, which flags the take in _stopping (the worker
    # would unwind it to CANCELLED). replicate + no backend_job_id keeps the backend cancel a
    # no-op, so no network is touched.
    tv.stop_rendering([g.id])
    assert g.id in jm._stopping

    # A view with NO JobManager (plain viewer / headless) offers nothing to stop and no-ops safely.
    tv_nojobs = TakesView(project, shot.id)
    assert "Stop rendering" not in [a.text() for a in tv_nojobs._build_context_menu([g.id]).actions()]
    tv_nojobs.stop_rendering([g.id])   # no jobs -> silent no-op, must not raise
    print("TakesView stop-rendering menu OK: generating-only, jobs-gated, routes to request_stop")


def test_cancel_remote_spend_on_terminal_failure() -> None:
    """A hosted take that FAILS terminally mid-render fires a best-effort cancel_prediction to
    stop remaining Replicate spend (card follow-up H4). Drives GenerationJob._run directly with
    a runner that raises and a stubbed replicate_client.cancel_prediction, asserting: cancel is
    attempted EXACTLY ONCE when the take is replicate + has a backend_job_id; NOT attempted when
    there's no backend_job_id or the backend is comfyui; the take still lands FAILED (the cancel
    only stops the bleed, it does not flip status to CANCELLED); a raising cancel is swallowed;
    and a DELIBERATE stop (_stopping) records CANCELLED and does NOT fire this path (request_stop
    already issued its own cancel)."""
    from backends import jobs as jobs_mod
    from backends import replicate_client

    calls: list[str] = []
    orig_cancel = replicate_client.cancel_prediction
    replicate_client.cancel_prediction = lambda pred_id, token=None: calls.append(pred_id)

    def make_job(project, take_id, backend, *, cancelled=None, stopping=None):
        sig = jobs_mod._JobSignals()
        done: list[tuple[str, str]] = []
        job = jobs_mod.GenerationJob(
            project, take_id, backend, lambda progress: (_ for _ in ()).throw(RuntimeError("boom")),
            sig, cancelled if cancelled is not None else set(),
            stopping if stopping is not None else set(), set(),
            lambda tid, status: done.append((tid, status)))
        return job, done

    try:
        # (a) replicate take with a backend_job_id -> cancel fired exactly once, take FAILED.
        project = Project.new()
        shot = project.add_shot("kick", model_id="seedance-2.0-std")
        t = project.add_take(shot.id, status=STATUS_PENDING,
                             settings_snapshot={"backend": "replicate"})
        project.update_take(t.id, backend_job_id="pred_abc")   # stamped mid-render by on_submit
        job, done = make_job(project, t.id, "replicate")
        job._run()
        assert calls == ["pred_abc"], calls
        assert project.get_take(t.id).status == STATUS_FAILED
        assert done == [(t.id, STATUS_FAILED)], done
        # The persisted job log must keep BOTH the error line and the cancel milestone: the
        # cancel's progress() re-writes the log from log_lines, so err has to be IN the list
        # (a `log_lines + [err]` copy-join would let the cancel line overwrite the error).
        rec = next(j for j in project._jobs.values() if j.take_id == t.id)
        assert "RuntimeError: boom" in (rec.log or ""), rec.log
        assert "requested cancel of prediction pred_abc" in (rec.log or ""), rec.log

        # (b) replicate take with NO backend_job_id (never submitted / create POST never returned)
        #     -> no cancel attempted (nothing to cancel), still FAILED.
        calls.clear()
        t2 = project.add_take(shot.id, status=STATUS_PENDING,
                              settings_snapshot={"backend": "replicate"})
        job, _ = make_job(project, t2.id, "replicate")
        job._run()
        assert calls == [], calls
        assert project.get_take(t2.id).status == STATUS_FAILED

        # (c) comfyui (local) take with a job id -> never cancelled via this hosted path.
        calls.clear()
        t3 = project.add_take(shot.id, status=STATUS_PENDING,
                              settings_snapshot={"backend": "comfyui"})
        project.update_take(t3.id, backend_job_id="prompt_123")
        job, _ = make_job(project, t3.id, "comfyui")
        job._run()
        assert calls == [], calls
        assert project.get_take(t3.id).status == STATUS_FAILED

        # (d) a raising cancel is swallowed - the worker still lands the take FAILED, no re-raise.
        calls.clear()
        def boom_cancel(pred_id, token=None):
            calls.append(pred_id)
            raise ConnectionError("cancel POST failed")
        replicate_client.cancel_prediction = boom_cancel
        t4 = project.add_take(shot.id, status=STATUS_PENDING,
                              settings_snapshot={"backend": "replicate"})
        project.update_take(t4.id, backend_job_id="pred_def")
        job, done = make_job(project, t4.id, "replicate")
        job._run()   # must not raise despite the cancel throwing
        assert calls == ["pred_def"], calls
        assert project.get_take(t4.id).status == STATUS_FAILED
        assert done == [(t4.id, STATUS_FAILED)], done

        # (e) a DELIBERATE stop (id in _stopping) records CANCELLED, not FAILED, and does NOT
        #     fire the terminal-failure cancel path (request_stop already cancelled remote spend).
        replicate_client.cancel_prediction = lambda pred_id, token=None: calls.append(pred_id)
        calls.clear()
        t5 = project.add_take(shot.id, status=STATUS_PENDING,
                              settings_snapshot={"backend": "replicate"})
        project.update_take(t5.id, backend_job_id="pred_stop")
        job, _ = make_job(project, t5.id, "replicate", stopping={t5.id})
        job._run()
        assert calls == [], calls   # cancel path not reached for a deliberate stop
        assert project.get_take(t5.id).status == STATUS_CANCELLED
    finally:
        replicate_client.cancel_prediction = orig_cancel

    print("cancel-remote-spend OK: fires once on replicate terminal failure w/ job id, "
          "skipped without id / for comfyui, cancel errors swallowed, deliberate stop excluded")


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


def test_shot_tab_missing_model_flag() -> None:
    """A shot whose model_id left the roster must show a disabled placeholder combo entry
    carrying the stored id and flag it red (mirroring the invalid-aspect state), NOT snap the
    combo to a real model at index 0. Picking a real model clears the flag and drops the
    placeholder. Card M9."""
    import library
    from PySide6.QtWidgets import QApplication

    from ui.shot_tab import ShotTab

    app = QApplication.instance() or QApplication([])  # noqa: F841
    project = Project.new()
    shot = project.add_shot("c", model_id="ghost-model-9000")   # not in the roster
    tab = ShotTab(project, shot)

    assert tab._current_model() is None, "unknown model resolves to None"
    assert not tab.model_valid(), "an off-roster model must be flagged invalid"
    assert "d9534f" in tab.model_combo.styleSheet(), "combo flagged red like an invalid aspect"
    assert tab.model_combo.currentData() == "ghost-model-9000", "combo holds the stored id, not index 0"
    idx = tab.model_combo.findData("ghost-model-9000")
    assert idx >= 0, "the missing model is a real combo entry"
    item = tab.model_combo.model().item(idx)
    assert item is not None and not item.isEnabled(), "placeholder entry is disabled (not user-pickable)"

    # Pick a real roster model -> flag clears, placeholder is dropped, combo lands on it.
    real = library.models()[0]["id"]
    tab.model_combo.setCurrentIndex(tab.model_combo.findData(real))
    assert tab.model_valid(), "picking a real model clears the invalid flag"
    assert "d9534f" not in tab.model_combo.styleSheet()
    assert tab.model_combo.findData("ghost-model-9000") < 0, "placeholder dropped after a real pick"

    # A BLANK model_id (a bare new shot) is NOT the missing-model case: it defaults to
    # index 0, stays valid, and gets no placeholder.
    blank = project.add_shot("blank")
    tab2 = ShotTab(project, blank)
    assert tab2.model_valid(), "a blank model_id defaults to the first roster model, not invalid"
    assert tab2.model_combo.currentIndex() == 0
    assert tab2._missing_model_idx is None, "no placeholder for a blank id"
    print("shot-tab missing-model OK: placeholder held + flagged red, real pick clears + drops it")


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


def test_takes_view_keyboard() -> None:
    """Keyboard triage in the takes grid (card UX4): Delete bins the selection, S toggles its
    star, Enter/Return opens the first selected take in the viewer. All route through the same
    delete/toggle_star/open_take_requested the mouse uses; a no-selection triage key is consumed
    but harmless; an unrelated key falls through (handle_grid_key returns False)."""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    from ui.takes_view import TakesView, _STAR_ROLE, _TakesListView

    app = QApplication.instance() or QApplication([])  # noqa: F841

    project = Project.new()
    shot = project.add_shot("kick", model_id="seedance-2.0-std")
    t1 = project.add_take(shot.id, status=STATUS_DONE)
    t2 = project.add_take(shot.id, status=STATUS_DONE)
    tv = TakesView(project, shot.id)
    assert isinstance(tv.view, _TakesListView)

    def select(*take_ids):
        sm = tv.view.selectionModel()
        sm.clear()
        for tid in take_ids:
            sm.select(tv.model.indexFromItem(tv._items[tid]),
                      sm.SelectionFlag.Select)

    # S on a selection toggles its star (write-through), in place - the item survives.
    select(t1.id)
    item1 = tv._items[t1.id]
    assert tv.handle_grid_key(int(Qt.Key.Key_S)) is True
    assert project.get_take(t1.id).starred is True
    assert tv._items[t1.id] is item1 and tv._items[t1.id].data(_STAR_ROLE) is True

    # Enter/Return opens the first selected take in the viewer (bubbles up to MainWindow).
    opened: list[str] = []
    tv.open_take_requested.connect(opened.append)
    select(t2.id, t1.id)
    assert tv.handle_grid_key(int(Qt.Key.Key_Return)) is True
    assert len(opened) == 1 and opened[0] in (t1.id, t2.id)   # ids[0] of the selection
    assert tv.handle_grid_key(int(Qt.Key.Key_Enter)) is True   # numpad Enter is also handled
    assert len(opened) == 2

    # Delete bins the whole selection (same neutralize+move_to_bin path as the menu).
    select(t1.id, t2.id)
    assert tv.handle_grid_key(int(Qt.Key.Key_Delete)) is True
    assert project.get_take(t1.id).deleted and project.get_take(t2.id).deleted
    assert tv.model.rowCount() == 0

    # A triage key with nothing selected is consumed (so it doesn't type-ahead search) but is
    # otherwise a no-op; a non-triage key falls through to QListView (returns False).
    select()
    assert tv.handle_grid_key(int(Qt.Key.Key_Delete)) is True
    assert tv.handle_grid_key(int(Qt.Key.Key_A)) is False
    print("TakesView keyboard OK: Delete bins, S stars, Enter opens; empty-sel no-op; other keys fall through")


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

    # (d) The queue-row right-click menu (no exec()): a PENDING take offers Cancel, a GENERATING
    #     take offers Stop rendering (UX1), and a mixed selection offers both.
    pending_id = next(t.id for t in qv.model.rows() if t.status == STATUS_PENDING)
    menu = qv._build_context_menu([pending_id])
    assert [a.text() for a in menu.actions()] == ["Cancel queued generation"]
    menu = qv._build_context_menu([g.id])                      # a GENERATING take: Stop rendering
    assert [a.text() for a in menu.actions()] == ["Stop rendering"]
    menu = qv._build_context_menu([pending_id, g.id])         # mixed: both entries present
    assert [a.text() for a in menu.actions()] == ["Cancel queued generation", "Stop rendering"]
    # Firing Stop rendering routes to JobManager.request_stop, which flags the generating take in
    # _stopping (the worker would then unwind it to CANCELLED). Use a REPLICATE take with no
    # backend_job_id so the backend cancel is a no-op - no live GPU/network is touched (a comfyui
    # stop would POST /interrupt to a down server and block on the socket timeout).
    gr = add(STATUS_GENERATING, backend="replicate")
    qv.refresh()
    qv._stop([gr.id])
    assert gr.id in jobs._stopping

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
    print("QueueView OK: zero per-row widgets, bounded child count, 1-row progress, coalesced rebuild, cancel + stop-rendering menu, done counter, last-finished strip, paint")


def test_recovery_banner_predicate() -> None:
    """The pure banner-text predicate: None below 1, present + correct singular/plural at 1+."""
    from ui.main_window import recovery_banner_text

    assert recovery_banner_text(0) is None
    assert recovery_banner_text(-1) is None
    one = recovery_banner_text(1)
    assert one is not None and "1 take was interrupted" in one, one
    many = recovery_banner_text(3)
    assert many is not None and "3 takes were interrupted" in many, many
    print("recovery banner predicate OK: none <1, singular/plural text")


def test_recovery_banner() -> None:
    """The one-time crash-recovery banner: hidden with no interrupted takes; shown (with the
    N-take message) when the project loads with interrupted takes; the Dismiss (x) hides it;
    a fresh window with zero interrupted takes stays hidden. Non-modal, so smoke never blocks."""
    from PySide6.QtWidgets import QApplication
    from store.models import STATUS_CANCELLED
    from ui.main_window import MainWindow, recovery_banner_text

    app = QApplication.instance() or QApplication([])  # noqa: F841

    # (a) clean project -> banner stays hidden.
    clean = Project.new()
    clean.add_shot("kick", model_id="seedance-2.0-std")
    win = MainWindow(clean)
    assert win.recovery_banner.isHidden(), "no interrupted takes -> banner hidden"

    # (b) a project that loads with interrupted takes -> banner shown with the right text.
    proj = Project.new()
    shot = proj.add_shot("kick", model_id="seedance-2.0-std")
    proj.add_take(shot.id, status=STATUS_CANCELLED, interrupted=True,
                  error="cancelled: ComfyUI/the app was closed mid-render")
    proj.add_take(shot.id, status=STATUS_CANCELLED, interrupted=True,
                  error="cancelled: ComfyUI/the app was closed mid-render")
    # a deliberately-cancelled take must NOT count toward the banner
    proj.add_take(shot.id, status=STATUS_CANCELLED, interrupted=False)
    win2 = MainWindow(proj)
    assert win2._interrupted_take_count() == 2, win2._interrupted_take_count()
    assert not win2.recovery_banner.isHidden(), "interrupted takes -> banner shown"
    assert win2.recovery_banner._label.text() == recovery_banner_text(2)
    assert "2 takes were interrupted" in win2.recovery_banner._label.text()

    # (c) binning an interrupted take from the card's grid keeps the banner honest: the
    #     count drops (a binned take leaves _interrupted_take_count), and binning the LAST
    #     one retires the banner entirely (reviewer finding: TakesView.delete doesn't route
    #     through restart/purge/recovery, so without the changed->sync hook it went stale).
    interrupted_ids = [t.id for t in proj.list_takes(shot.id) if t.interrupted]
    card = win2.cards[shot.id]
    card.expand_btn.setChecked(True)          # creates the card's TakesView (changed is wired)
    card.takes_view.delete([interrupted_ids[0]])
    assert not win2.recovery_banner.isHidden(), "one interrupted take left -> banner stays"
    assert win2.recovery_banner._label.text() == recovery_banner_text(1)  # count re-synced
    card.takes_view.delete([interrupted_ids[1]])
    assert win2.recovery_banner.isHidden(), "binned the last interrupted take -> banner retires"

    # (d) the Dismiss (x) hides the banner, and a later take-churn sync must NOT re-arm it.
    from PySide6.QtWidgets import QPushButton
    win2._refresh_recovery_banner()           # re-arm is a no-op now (count 0) - banner stays down
    assert win2.recovery_banner.isHidden()
    proj.add_take(shot.id, status=STATUS_CANCELLED, interrupted=True,
                  error="cancelled: ComfyUI/the app was closed mid-render")
    win2._refresh_recovery_banner()           # simulate a recovery completion arming it again
    assert not win2.recovery_banner.isHidden()
    close_btn = next(b for b in win2.recovery_banner.findChildren(QPushButton)
                     if b.objectName() == "infoBannerClose")
    close_btn.click()
    assert win2.recovery_banner.isHidden(), "Dismiss -> banner hidden"
    win2._sync_recovery_banner()              # take-churn sync respects the dismissal
    assert win2.recovery_banner.isHidden(), "sync must never re-arm a dismissed banner"

    print("recovery banner OK: hidden clean, shown with N-take text on interrupted load, "
          "bin re-counts/retires it, Dismiss hides + sync never re-arms, "
          "deliberate cancel excluded")


if __name__ == "__main__":
    test_recovery_banner_predicate()
    test_recovery_banner()
    test_bin_restore()
    test_move_to_bin_partial_failure()
    test_takes_view()
    test_takes_view_incremental_update()
    test_take_star_badge()
    test_take_star_toggle_incremental()
    test_takes_view_keyboard()
    test_take_progress_label()
    test_take_tile_tooltip()
    test_bin_neutralizes_queued_take()
    test_takes_view_stop_rendering_menu()
    test_cancel_remote_spend_on_terminal_failure()
    test_assets_view()
    test_replace_background_ui()
    test_transparent_import_forces_bg()
    test_asset_picker()
    test_card_and_window()
    test_framed_row_thumbs()
    test_runner_uses_snapshot_not_live_shot()
    test_snapshot_detached_from_live_shot_at_creation()
    test_param_enum_preserves_out_of_schema_value()
    test_shot_tab_missing_model_flag()
    test_queue_view()
    print("PHASE 4 SMOKE: PASS")
