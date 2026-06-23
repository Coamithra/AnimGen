"""Takes folder view - the per-shot grid of generated takes.

A QListView in IconMode (Windows-folder style) with an icon-size slider, a
favorite/all filter, live status badges, per-take star + delete-to-bin, and
shift/ctrl multi-select. Thumbnails are the first video frame, generated lazily and
cached. Emits `changed` (so the card header can refresh counts) and `export_requested`.

The preview height auto-fits the rows its takes actually occupy (1.._MAX_PREVIEW_ROWS),
so a single row doesn't reserve an empty second row; a thin handle below the grid lets the
user drag an explicit height (double-click it to return to auto-fit).
"""
from __future__ import annotations

import threading
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor, QIcon, QPainter, QPixmap, QStandardItem, QStandardItemModel,
)
from PySide6.QtWidgets import (
    QComboBox, QFrame, QHBoxLayout, QLabel, QListView, QMenu, QPushButton, QSlider,
    QStyledItemDelegate, QVBoxLayout, QWidget,
)

from pipeline import extract, takes_io
from store.project import Project
from ui.take_player import decode_strip, take_source

_ANIM_INTERVAL_MS = 80     # ~12.5 fps grid loop (a thumbnail only needs to read as motion)

_USER_ROLE = int(Qt.ItemDataRole.UserRole)
_STAR_ROLE = int(Qt.ItemDataRole.UserRole) + 1   # bool: take.starred, read by the star delegate
_STAR_BADGE = 20                                 # px square clickable star badge, top-left of a cell
_BADGE = {"pending": "⏳", "generating": "▶", "done": "", "failed": "✗"}
_BADGE_COLOR = {"pending": "#b0b0b0", "generating": "#5aa0ff",
                "done": "#7ade8c", "failed": "#ff6b6b", "cancelled": "#c0a060"}

# Grid cell = icon side + this padding (room for the label/badge under the thumb and a
# little horizontal breathing room). Shared by the grid-size and preview-height math so
# they stay in lockstep.
_GRID_PAD_W = 26
_GRID_PAD_H = 26      # one short label line under the thumb - was 42, which left a wide
                      # whitespace band below each row; 26 packs the rows tighter.
_VIEW_SPACING = 8     # QListView.setSpacing - margin around each grid cell
_VIEW_FRAME_PAD = 4   # QListView frame border (top + bottom), so rows aren't clipped by it
_MAX_PREVIEW_ROWS = 2   # auto-fit cap: the preview shrinks to fit its takes but never grows
                        # past this on its own (more takes scroll inside); manual drag can exceed.
_DRAG_MAX_ROWS = 8      # ceiling for a manual drag-resize, so the panel can't be dragged absurdly tall


def preview_height(icon_size: int, rows: float = _MAX_PREVIEW_ROWS) -> int:
    """Pixel height for the takes preview list: `rows` grid rows of `icon_size` icons, plus
    the list's spacing/frame padding. Pure so it's unit-testable headlessly. The list keeps
    its own scrollbar, so takes beyond `rows` scroll inside this window.

    Spacing falls between rows as well as above/below them, so N rows need (N+1) gaps;
    erring slightly tall keeps whole rows from being clipped before the scrollbar kicks in."""
    grid_h = icon_size + _GRID_PAD_H
    return round(grid_h * rows) + round(_VIEW_SPACING * (rows + 1)) + _VIEW_FRAME_PAD


def progress_percent(frac: float) -> str:
    """Compact percentage string for a take's render fraction, e.g. 0.45 -> "45%". Clamped
    to [0,1] first. Pure so it's unit-testable headlessly."""
    return f"{round(max(0.0, min(1.0, frac)) * 100)}%"


def take_tile_label(status: str, take_id: str, pct: str = "") -> str:
    """Label under a take's grid tile: a status badge and a tail. The star is shown as a
    clickable badge on the thumbnail (star delegate), not prefixed here.

    A still-generating take whose live render fraction is known shows that percent (`pct`,
    the same number the Queue tab's progress bar reports) in place of the bare "generating"
    word; everything else is unchanged. Pure so it's unit-testable headlessly."""
    badge = _BADGE.get(status, "")
    if status == "generating":
        tail = f"  {pct}" if pct else f"  {status}"
    elif status == "done":
        tail = ""
    else:
        tail = f"  {status}"
    return f"{badge}{tail}".strip() or take_id[:6]


def columns_for(viewport_width: int, icon_size: int) -> int:
    """How many grid cells fit across a viewport `viewport_width` px wide. Returns 0 when the
    width isn't known yet (view not laid out / headless), signalling 'can't tell'. Pure."""
    if viewport_width <= 0:
        return 0
    step = (icon_size + _GRID_PAD_W) + _VIEW_SPACING
    return max(1, (viewport_width - _VIEW_SPACING) // step)


def rows_for(n_items: int, viewport_width: int, icon_size: int,
             max_rows: int = _MAX_PREVIEW_ROWS) -> int:
    """Number of grid rows the preview should be tall to fit `n_items` without wasting space,
    clamped to [1, max_rows]. With the width unknown we fall back to the full cap so nothing is
    clipped before layout settles. Pure so it's unit-testable headlessly."""
    cols = columns_for(viewport_width, icon_size)
    if cols == 0:
        return max_rows
    needed = -(-max(n_items, 1) // cols)   # ceil division
    return max(1, min(needed, max_rows))


def star_badge_rect(cell: QRect) -> QRect:
    """Hot-zone + paint rect for a take's clickable star badge: the top-left corner of its
    grid cell, inset slightly. Paint and click-detection share this so the visible badge is
    exactly what's clickable. Pure (QRect in -> QRect out) so it's unit-testable headlessly."""
    return QRect(cell.left() + 4, cell.top() + 2, _STAR_BADGE, _STAR_BADGE)


class _StarDelegate(QStyledItemDelegate):
    """Draws a clickable ★/☆ badge in the top-left of each take cell and toggles that take's
    star when the badge is clicked - so starring matches the shot cards' click-a-star flow
    instead of needing the right-click menu. Click detection and painting share
    `star_badge_rect`; the actual toggle is delegated to `on_toggle(take_id)` so the view keeps
    ownership of persistence. A badge click is swallowed (press/release/double-click) so it
    never also changes the selection or opens the viewer."""

    def __init__(self, on_toggle):
        super().__init__()
        self._on_toggle = on_toggle

    def paint(self, painter, option, index):  # noqa: N802 - Qt override
        super().paint(painter, option, index)
        rect = star_badge_rect(option.rect)
        starred = bool(index.data(_STAR_ROLE))
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(0, 0, 0, 110))   # translucent disc so the glyph reads on any thumb
        painter.drawEllipse(rect)
        painter.setPen(QColor("#f2c14e") if starred else QColor("#dddddd"))
        font = painter.font()
        font.setPointSizeF(max(9.0, rect.height() * 0.62))
        painter.setFont(font)
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, "★" if starred else "☆")
        painter.restore()

    def editorEvent(self, event, model, option, index):  # noqa: N802 - Qt override
        et = event.type()
        if et in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease,
                  QEvent.Type.MouseButtonDblClick):
            if (event.button() == Qt.MouseButton.LeftButton
                    and star_badge_rect(option.rect).contains(event.position().toPoint())):
                if et == QEvent.Type.MouseButtonRelease:
                    take_id = index.data(_USER_ROLE)
                    if take_id:
                        self._on_toggle(take_id)
                return True                       # swallow so it doesn't select / open the viewer
        return super().editorEvent(event, model, option, index)


class _StripLoader(QObject):
    """Decodes each take's clip into a small frame strip off the GUI thread, emitting one
    `ready` per take as it finishes (so tiles start animating progressively rather than all
    at once). `gen` is a generation token the view uses to discard results from a load that
    has since been superseded (e.g. the row was collapsed and re-expanded)."""
    ready = Signal(str, list, int)   # take_id, list[QImage], gen

    def __init__(self, jobs: list, gen: int):
        super().__init__()
        self._jobs = jobs            # list[(take_id, source_path)]
        self._gen = gen

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        for take_id, source in self._jobs:
            try:
                frames = decode_strip(source)
            except Exception:  # noqa: BLE001 - a bad clip just doesn't animate
                frames = []
            if frames:
                self.ready.emit(take_id, frames, self._gen)


class _ResizeHandle(QFrame):
    """A thin grab bar under the preview grid. Drag it to set an explicit panel height
    (overriding auto-fit); double-click to return to auto-fit. It only talks to its owner
    TakesView through `current_view_height`/`set_manual_height`/`clear_manual_height`, so
    the height policy stays in one place."""
    def __init__(self, owner: "TakesView"):
        super().__init__()
        self._owner = owner
        self.setFixedHeight(7)
        self.setCursor(Qt.CursorShape.SizeVerCursor)
        self.setObjectName("takesResizeHandle")
        self.setStyleSheet(
            "#takesResizeHandle { background:#3a3f4b; border-radius:3px; }"
            "#takesResizeHandle:hover { background:#4a5160; }")
        self.setToolTip("Drag to resize · double-click to auto-fit")
        self._press_y: float | None = None
        self._base_h = 0

    def mousePressEvent(self, event):  # noqa: N802 - Qt override
        self._press_y = event.globalPosition().y()
        self._base_h = self._owner.current_view_height()
        event.accept()

    def mouseMoveEvent(self, event):  # noqa: N802 - Qt override
        if self._press_y is None:
            return
        if not (event.buttons() & Qt.MouseButton.LeftButton):
            self._press_y = None    # release was missed (off-widget / focus stolen) - drop the drag
            return
        delta = int(event.globalPosition().y() - self._press_y)
        self._owner.set_manual_height(self._base_h + delta)
        event.accept()

    def mouseReleaseEvent(self, event):  # noqa: N802 - Qt override
        self._press_y = None
        event.accept()

    def mouseDoubleClickEvent(self, event):  # noqa: N802 - Qt override
        self._owner.clear_manual_height()
        event.accept()


class TakesView(QWidget):
    changed = Signal()
    export_requested = Signal(list)   # list[take_id]
    open_take_requested = Signal(str)  # take_id -> open it in the frame-by-frame viewer tab
    restart_requested = Signal(list)   # list[take_id] -> re-run cancelled / crash-interrupted-failed takes (MainWindow drives it)

    def __init__(self, project: Project, shot_id: str, jobs=None):
        super().__init__()
        self.project = project
        self.shot_id = shot_id
        self.jobs = jobs
        self._latest_pct: dict[str, float] = {}       # take_id -> live render fraction 0..1
        self._items: dict[str, QStandardItem] = {}    # take_id -> grid item (for live frames)
        self._icon_cache: dict[str, tuple] = {}       # take_id -> (signature, QIcon); skip disk re-decode
        self._strips: dict[str, list] = {}            # take_id -> list[QPixmap] (decoded loop)
        self._strip_pending: set[str] = set()         # take_ids with an in-flight single-take strip decode
        self._frame_idx: dict[str, int] = {}          # take_id -> current frame in its strip
        self._animating = True
        self._anim_gen = 0                            # bumped on each (re)load to drop stale strips
        self._loader: _StripLoader | None = None
        self._user_height: int | None = None          # set by a manual drag-resize; overrides auto-fit
        self._anim_timer = QTimer(self)
        self._anim_timer.setInterval(_ANIM_INTERVAL_MS)
        self._anim_timer.timeout.connect(self._tick)
        self._build()
        self.load()
        if jobs is not None:
            # Mirror the Queue tab: surface a local render's live % on the generating tile.
            jobs.progress_pct.connect(self._on_progress_pct)

    def _build(self) -> None:
        self.filter = QComboBox()
        self.filter.addItems(["All", "Favorites"])
        self.filter.currentIndexChanged.connect(self.load)

        self.size_slider = QSlider(Qt.Orientation.Horizontal)
        self.size_slider.setRange(80, 320)
        self.size_slider.setValue(140)
        self.size_slider.setFixedWidth(120)
        self.size_slider.valueChanged.connect(self._apply_icon_size)

        export_btn = QPushButton("Export selected")
        export_btn.clicked.connect(
            lambda: self.export_requested.emit(self.selected_take_ids()))
        self.count_label = QLabel("")

        head = QHBoxLayout()
        head.addWidget(QLabel("View:")); head.addWidget(self.filter)
        head.addStretch(1)
        head.addWidget(self.count_label)
        head.addWidget(QLabel("Size:")); head.addWidget(self.size_slider)
        head.addWidget(export_btn)

        self.model = QStandardItemModel()
        self.view = QListView()
        self.view.setModel(self.model)
        self.view.setViewMode(QListView.ViewMode.IconMode)
        self.view.setResizeMode(QListView.ResizeMode.Adjust)
        self.view.setMovement(QListView.Movement.Static)
        self.view.setSelectionMode(QListView.SelectionMode.ExtendedSelection)
        self.view.setWordWrap(True)
        self.view.setSpacing(_VIEW_SPACING)
        self.view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self._context_menu)
        self.view.doubleClicked.connect(self._open_in_viewer)
        self._star_delegate = _StarDelegate(self._toggle_star_by_id)
        self.view.setItemDelegate(self._star_delegate)
        self._apply_icon_size()

        self.resize_handle = _ResizeHandle(self)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 0, 6, 6)
        lay.addLayout(head)
        lay.addWidget(self.view)
        lay.addWidget(self.resize_handle)

    # ---- population -----------------------------------------------------
    def load(self) -> None:
        fav = self.filter.currentText() == "Favorites"
        takes = self.project.list_takes(self.shot_id, starred_only=fav)
        live = {t.id for t in takes}                  # drop cached pcts/icons for evicted takes
        self._latest_pct = {k: v for k, v in self._latest_pct.items() if k in live}
        self._icon_cache = {k: v for k, v in self._icon_cache.items() if k in live}
        self._reset_anim()
        self.model.clear()
        self._items.clear()
        for t in takes:
            item = QStandardItem(self._icon_for(t), self._label(t))
            item.setData(t.id, _USER_ROLE)
            item.setData(bool(t.starred), _STAR_ROLE)
            item.setEditable(False)
            self.model.appendRow(item)
            self._items[t.id] = item
        self.count_label.setText(f"{len(takes)} shown")
        self._apply_height()
        if self._animating:
            self._start_strip_load(takes)

    def update_take(self, take_id: str) -> None:
        """Refresh a single take's tile in place on a status signal - the cheap sibling of the
        full model.clear()+reload load() does. A plain status transition keeps the same rows, so
        the common per-signal path (including the status_changed+finished double-fire on
        completion, and every take's transitions during a batch) updates just one item's
        badge/%/star/thumbnail instead of recreating N items and re-decoding N thumbnails+strips
        (card #75). Falls back to load() only when the take's membership in the current view
        actually changed: a newly-visible take, a deletion, or a filter boundary crossed."""
        fav = self.filter.currentText() == "Favorites"
        t = self.project.get_take(take_id)
        # Mirror list_takes() (what load() uses): a soft-deleted take is never shown, and under
        # the Favorites filter only starred takes are.
        should_show = t is not None and not t.deleted and (not fav or t.starred)
        item = self._items.get(take_id)
        if item is None:
            if should_show:
                self.load()       # queued / now-matching take not yet in the grid
            return                # correctly absent - nothing to update
        if not should_show:
            self.load()           # take deleted or no longer matches the filter
            return
        item.setText(self._label(t))
        item.setData(bool(t.starred), _STAR_ROLE)
        if take_id not in self._strips and take_id not in self._strip_pending:
            # Not animating yet: refresh the static thumbnail, and if the take just became
            # playable (e.g. it finished rendering) kick off a single-take strip decode so it
            # starts looping - without re-decoding every other take's strip the way load() would.
            item.setIcon(self._icon_for(t))
            if self._animating and take_source(t):
                self._strip_pending.add(take_id)
                self._start_strip_load([t])

    # ---- animated previews (decoded frame loop over the static thumbnails) ----
    # The grid tiles animate by cycling a small decoded frame strip per take. This goes
    # through PyAV (decode_strip) rather than QMovie so it animates the real .mp4 renders -
    # we don't generate gif previews, so a gif-only path would leave every actual take
    # static. Strips are held only while the row is expanded (cleared on collapse) to bound
    # memory; re-expanding re-decodes off-thread.
    def _reset_anim(self) -> None:
        self._anim_gen += 1          # any in-flight loader's results now belong to an old gen
        self._anim_timer.stop()
        self._strips.clear()
        self._frame_idx.clear()
        self._strip_pending.clear()
        self._loader = None

    def _start_strip_load(self, takes) -> None:
        jobs = []
        for t in takes:
            src = take_source(t)
            if src:
                jobs.append((t.id, src))
        if not jobs:
            return
        # Held on self only so a freshly-started loader isn't GC'd before its thread runs; a
        # single-take update_take decode may overwrite this ref while a full-load decode is still
        # in flight, which is fine - the running thread keeps the older loader alive via its bound
        # _run, both share _anim_gen, so both deliver. Concurrent loaders are intentionally
        # fire-and-forget; don't "fix" the overwrite.
        self._loader = _StripLoader(jobs, self._anim_gen)
        self._loader.ready.connect(self._on_strip_ready)
        self._loader.start()

    def _on_strip_ready(self, take_id: str, qimages: list, gen: int) -> None:
        self._strip_pending.discard(take_id)
        if gen != self._anim_gen or take_id not in self._items:
            return                                          # superseded by a newer load
        self._strips[take_id] = [QPixmap.fromImage(im) for im in qimages]
        self._frame_idx[take_id] = 0
        self._items[take_id].setIcon(QIcon(self._strips[take_id][0]))
        if self._animating and not self._anim_timer.isActive():
            self._anim_timer.start()

    def _tick(self) -> None:
        for take_id, strip in self._strips.items():
            item = self._items.get(take_id)
            if not (item and strip):
                continue
            idx = (self._frame_idx.get(take_id, 0) + 1) % len(strip)
            self._frame_idx[take_id] = idx
            item.setIcon(QIcon(strip[idx]))

    def set_animating(self, on: bool) -> None:
        """Play/pause the grid animations - the card pauses them while collapsed so a long
        shot list isn't decoding clips no one is looking at. Collapsing also frees the
        decoded strips (re-decoded on re-expand) to keep memory to the visible row."""
        if on == self._animating:
            return
        self._animating = on
        if on:
            if not self._strips:
                self._start_strip_load(self.project.list_takes(
                    self.shot_id, starred_only=self.filter.currentText() == "Favorites"))
            elif not self._anim_timer.isActive():
                self._anim_timer.start()
        else:
            self._reset_anim()

    def hideEvent(self, event):  # noqa: N802 - Qt override: pause when the view goes off screen
        self.set_animating(False)
        super().hideEvent(event)

    def showEvent(self, event):  # noqa: N802 - Qt override
        self.set_animating(True)
        super().showEvent(event)
        self._apply_height()   # viewport width is known once shown -> recompute columns/rows

    def resizeEvent(self, event):  # noqa: N802 - Qt override: width change -> different column count
        super().resizeEvent(event)
        # _apply_height pins the view's height, which can feed back here; it settles in one
        # pass because rows_for is idempotent for a given width (same width -> same height).
        self._apply_height()

    def _label(self, t) -> str:
        pct = ""
        if t.status == "generating":
            frac = self._latest_pct.get(t.id)
            if frac is not None:
                pct = progress_percent(frac)
        return take_tile_label(t.status, t.id, pct)

    def _on_progress_pct(self, take_id: str, frac: float, label: str) -> None:
        """Live render fraction for a take: relabel just its tile in place (no reload, which
        would reset the running thumbnail animation). Ignored unless the take is one of ours and
        still generating — a documented progress_state tail keeps arriving ~20-30s after a local
        render completes (rule #11) and must not relabel an already-finished tile. `label` (the
        Queue tab's "step N/M" text) has no surface here, so only the fraction is kept."""
        item = self._items.get(take_id)
        if item is None:
            return                                    # not in this shot's grid (other shot / filtered out)
        t = self.project.get_take(take_id)
        if t is None or t.status != "generating":
            return
        self._latest_pct[take_id] = frac
        item.setText(self._label(t))

    def _icon_for(self, t) -> QIcon:
        """The take's static tile icon, cached per take by a content signature (thumbnail path +
        mtime, else status placeholder) so a repeated load() / status update doesn't re-decode
        the same image off disk each time (card #75). The animation strip, once decoded, paints
        over this; the cache only covers the static fill and non-animating tiles."""
        key = self._icon_cache_key(t)
        cached = self._icon_cache.get(t.id)
        if cached is not None and cached[0] == key:
            return cached[1]
        icon = self._build_icon(t)
        self._icon_cache[t.id] = (key, icon)
        return icon

    def _icon_cache_key(self, t) -> tuple:
        """What `_icon_for`'s result depends on: the thumbnail file (path + mtime) if present,
        else the source video path, else the status placeholder. A change here invalidates the
        cached QIcon."""
        if t.thumbnail and Path(t.thumbnail).exists():
            try:
                return ("thumb", t.thumbnail, Path(t.thumbnail).stat().st_mtime_ns)
            except OSError:
                return ("thumb", t.thumbnail, None)
        if t.video_path and Path(t.video_path).exists():
            return ("video", t.video_path)
        return ("placeholder", t.status)

    def _build_icon(self, t) -> QIcon:
        if t.thumbnail and Path(t.thumbnail).exists():
            return QIcon(t.thumbnail)
        if t.video_path and Path(t.video_path).exists():
            try:
                out = self.project.thumbs_dir / f"{t.id}.png"
                if not out.exists():
                    extract.make_thumbnail(t.video_path, out)
                if out.exists():
                    self.project.update_take(t.id, thumbnail=str(out))
                    return QIcon(str(out))
            except Exception:  # noqa: BLE001 - corrupt/locked video -> placeholder
                pass
        return self._placeholder(t.status)

    def _placeholder(self, status: str) -> QIcon:
        pm = QPixmap(220, 160)
        pm.fill(QColor("#222"))
        p = QPainter(pm)
        p.setPen(QColor(_BADGE_COLOR.get(status, "#999")))
        p.drawText(pm.rect(), Qt.AlignmentFlag.AlignCenter, status)
        p.end()
        return QIcon(pm)

    def _apply_icon_size(self) -> None:
        s = self.size_slider.value()
        self.view.setIconSize(QSize(s, s))
        self.view.setGridSize(QSize(s + _GRID_PAD_W, s + _GRID_PAD_H))
        self._apply_height()

    # ---- preview height: auto-fit to content, with a manual drag override ----
    def _apply_height(self) -> None:
        """Pin the preview height. A manual drag wins; otherwise auto-fit to the rows the
        current takes actually occupy (1.._MAX_PREVIEW_ROWS), so a single row of takes
        doesn't reserve an empty second row."""
        s = self.size_slider.value()
        if self._user_height is not None:
            self.view.setFixedHeight(self._user_height)
            return
        rows = rows_for(self.model.rowCount(), self.view.viewport().width(), s)
        self.view.setFixedHeight(preview_height(s, rows))

    def current_view_height(self) -> int:
        return self.view.height()

    def set_manual_height(self, height: int) -> None:
        """Pin an explicit height from a drag-resize, clamped to [1, _DRAG_MAX_ROWS] rows."""
        s = self.size_slider.value()
        lo, hi = preview_height(s, 1), preview_height(s, _DRAG_MAX_ROWS)
        self._user_height = max(lo, min(height, hi))
        self.view.setFixedHeight(self._user_height)

    def clear_manual_height(self) -> None:
        """Drop the manual override and return to auto-fit."""
        self._user_height = None
        self._apply_height()

    # ---- selection / actions -------------------------------------------
    def selected_take_ids(self) -> list:
        return [self.model.itemFromIndex(i).data(_USER_ROLE)
                for i in self.view.selectedIndexes()]

    def all_take_ids(self) -> list:
        return [self.model.item(r).data(_USER_ROLE) for r in range(self.model.rowCount())]

    def _context_menu(self, pos) -> None:
        ids = self.selected_take_ids()
        if not ids:
            return
        self._build_context_menu(ids).exec(self.view.mapToGlobal(pos))

    def _build_context_menu(self, ids: list) -> QMenu:
        """Build the per-take right-click menu with its actions wired (no exec()), so the menu
        and its wiring are headless-testable (rule #4 / the shot_card pattern). A Restart entry
        appears when the selection holds a cancelled take or a crash-interrupted FAILED take (an
        in-flight render lost to an app/ComfyUI death); it bubbles up to MainWindow, which owns
        the cost gate + queue."""
        menu = QMenu(self)
        menu.addAction("Open in viewer").triggered.connect(self._open_in_viewer)
        menu.addAction("Open in external player").triggered.connect(self._open_selected)
        menu.addSeparator()
        restartable = [tid for tid in ids
                       if (t := self.project.get_take(tid))
                       and (t.status == "cancelled"
                            or (t.status == "failed" and t.interrupted))]
        if restartable:
            label = "Restart take" if len(restartable) == 1 else f"Restart {len(restartable)} takes"
            menu.addAction(label).triggered.connect(
                lambda: self.restart_requested.emit(restartable))
            menu.addSeparator()
        menu.addAction("Toggle star").triggered.connect(lambda: self.toggle_star(ids))
        menu.addAction("Delete (to bin)").triggered.connect(lambda: self.delete(ids))
        menu.addAction("Export selected").triggered.connect(
            lambda: self.export_requested.emit(ids))
        return menu

    def toggle_star(self, ids: list) -> None:
        for tid in ids:
            t = self.project.get_take(tid)
            if t:
                self.project.set_starred(tid, not t.starred)
                # Incremental in-place refresh, not load()'s model.clear()+rebuild + every-take PyAV
                # strip re-decode, which froze the UI for seconds on a many-take row. update_take
                # owns the membership-cross fallback to load() (card #75 incremental path).
                self.update_take(tid)
        self.changed.emit()

    def _toggle_star_by_id(self, take_id: str) -> None:
        """Flip one take's star (from the clickable badge). Write-through via toggle_star, so
        it persists instantly - same as the right-click 'Toggle star'."""
        self.toggle_star([take_id])

    def delete(self, ids: list) -> None:
        for tid in ids:
            t = self.project.get_take(tid)
            if t:
                takes_io.move_to_bin(t, self.project)
        self.load()
        self.changed.emit()

    def _open_in_viewer(self, *_) -> None:
        """Open the (first) selected take in the in-app frame-by-frame viewer tab. Bubbles
        up to MainWindow, which owns the tab widget."""
        ids = self.selected_take_ids()
        if ids:
            self.open_take_requested.emit(ids[0])

    def _open_selected(self, *_) -> None:
        import os
        for tid in self.selected_take_ids():
            t = self.project.get_take(tid)
            if t and t.video_path and Path(t.video_path).exists():
                try:
                    os.startfile(t.video_path)  # type: ignore[attr-defined]  # Windows
                except Exception:  # noqa: BLE001
                    pass
