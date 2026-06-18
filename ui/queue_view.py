"""Queue tab - a live view of every generation in flight.

The generation queue (backends/jobs.JobManager) runs hosted jobs a few in parallel and
local jobs one at a time, streaming free-text progress lines into the main log. That log
interleaves every job's output keyed only by an 8-char take-id, so it's hard to see *what's
running, what's queued behind it, and how far along each is*. This tab assembles that into a
list: one row per active (queued / generating) take plus the most-recently finished ones,
each showing its shot, model, backend, status and progress.

**No per-row widgets (rule #18).** This is a `QTableView` over a `QAbstractTableModel`; the
Progress column is **delegate-painted** (`QStyle.CE_ProgressBar`) - a determinate bar is
drawn only on a still-rendering local (ComfyUI) take, everything else is plain text. The old
implementation was a `QTableWidget` that rebuilt every row's cells *and* put a live
`QProgressBar` + `QPushButton('Cancel')` on every pending/generating row on every signal; a
mass-cancel (`cancel_pending`/`abandon_local` emit `status_changed` per take) then constructed
tens of thousands of `QProgressBar`s in one event-loop turn, the overlapping-children pileup
that overflowed `QWidgetPrivate::paintSiblingsRecursive` (the rule #18 native crash). With zero
per-row widgets the child count is bounded regardless of queue depth, so that path is gone.

Two more anti-stutter measures: the three structural signals (status_changed / finished /
failed) are **coalesced** through a 0-delay timer into a single rebuild per event-loop turn
(a completion no longer triggers ~3 rebuilds; a mass-cancel storm collapses to one), and the
high-frequency progress / progress_pct signals update **just the one row's Progress cell** in
place (`dataChanged`) rather than rebuilding the table. Per-take cancel of a queued take moved
to the row's right-click menu (the bulk *Cancel pending* button stays in the header).

It owns no real state of its own beyond a cache of each take's latest progress line and
fraction. Rows are derived from project.list_takes() by status; the queue engine is untouched.
"""
from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QHBoxLayout, QHeaderView, QLabel, QMenu, QPushButton,
    QStyle, QStyledItemDelegate, QStyleOptionProgressBar, QTableView, QToolButton,
    QVBoxLayout, QWidget,
)

import library
from store.project import Project
from store.models import (
    STATUS_CANCELLED, STATUS_DONE, STATUS_FAILED, STATUS_GENERATING, STATUS_PENDING,
)

_COLUMNS = ["Shot", "Model", "Backend", "Status", "Progress"]
_SHOT_COL, _MODEL_COL, _BACKEND_COL, _STATUS_COL, _PROGRESS_COL = range(len(_COLUMNS))
_RECENT_LIMIT = 15      # how many finished takes to keep visible below the active ones
_FINISHED = (STATUS_DONE, STATUS_FAILED, STATUS_CANCELLED)

# custom item-data roles the Progress delegate reads (an int 0..100 + label when the cell
# should paint a determinate bar; None bar-role => the delegate falls back to plain text).
_BAR_ROLE = int(Qt.ItemDataRole.UserRole)
_BAR_LABEL_ROLE = int(Qt.ItemDataRole.UserRole) + 1

# status value -> (label shown in the Status column, row tint)
_STATUS_DISPLAY = {
    STATUS_GENERATING: ("running", QColor("#1a7f37")),
    STATUS_PENDING:    ("queued",  QColor("#57606a")),
    STATUS_DONE:       ("done",    QColor("#1a7f37")),
    STATUS_FAILED:     ("failed",  QColor("#cf222e")),
    STATUS_CANCELLED:  ("cancelled", QColor("#57606a")),
}


def _elapsed(created: str, completed: str) -> str:
    """Human elapsed time between two second-precision ISO timestamps, e.g. "3m15s".

    Both stamps come from store.project._now() / jobs.GenerationJob (no timezone), so a
    plain fromisoformat diff is safe. Returns "" if either is missing/unparseable or the
    span is negative (clock skew), so the caller falls back to a bare "done"."""
    if not created or not completed:
        return ""
    try:
        secs = int((datetime.fromisoformat(completed)
                    - datetime.fromisoformat(created)).total_seconds())
    except ValueError:
        return ""
    if secs < 0:
        return ""
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m}m{s}s"
    if m:
        return f"{m}m{s}s"
    return f"{s}s"


def done_elapsed(take) -> str:
    """Render-duration label for a finished take: started -> completed.

    Falls back to created -> completed only for takes generated before `started` was
    recorded. Using `created` for current takes would be wrong: the serialized local queue
    stamps every take's `created` at batch launch, so a later take's created -> completed
    span is its cumulative queue wait, not how long it actually rendered."""
    return _elapsed(take.started or take.created, take.completed)


def select_rows(takes, dismissed: "frozenset[str] | set[str]" = frozenset(),
                recent_limit: int = _RECENT_LIMIT) -> list:
    """The takes the queue shows: every active (generating/pending) take first, in queue
    order, then the most-recently *added* finished ones (capped at recent_limit), reversed
    so the latest is on top (takes arrive in list_takes() creation order, not completion order).

    Finished takes whose id is in `dismissed` are filtered out — that's what the Clear
    button does. Active takes are never dismissable, so a still-queued or in-flight take
    always shows even if its id somehow lands in `dismissed`."""
    active = [t for t in takes if t.status == STATUS_GENERATING]
    active += [t for t in takes if t.status == STATUS_PENDING]
    finished = [t for t in takes if t.status in _FINISHED and t.id not in dismissed]
    return active + finished[-recent_limit:][::-1]   # latest-added finished on top


def _model_backend(take) -> tuple[str, str]:
    snap = take.settings_snapshot or {}
    model_id = snap.get("model_id", "")
    m = library.get_model(model_id)
    name = m["display_name"] if m else (model_id or "?")
    return name, snap.get("backend", "") or (m["backend"] if m else "")


class QueueModel(QAbstractTableModel):
    """Read-only table model over the queue's takes. Holds the ordered row list plus the
    per-take progress-line / fraction caches, and exposes the Progress cell to the delegate
    through the `_BAR_ROLE` / `_BAR_LABEL_ROLE` custom roles. No widgets - the whole point."""

    def __init__(self, project: Project, parent=None):
        super().__init__(parent)
        self.project = project
        self._rows: list = []                 # ordered Takes currently shown
        self._row_index: dict[str, int] = {}  # take_id -> row, for granular dataChanged
        self._latest: dict[str, str] = {}              # take_id -> last progress line
        self._latest_pct: dict[str, tuple[float, str]] = {}   # take_id -> (fraction, label)
        self._dismissed: set[str] = set()              # finished take_ids hidden via Clear

    # ---- Qt model API ---------------------------------------------------
    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: N802 - Qt override
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QModelIndex()) -> int:  # noqa: N802 - Qt override
        return 0 if parent.isValid() else len(_COLUMNS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):  # noqa: N802
        if (orientation == Qt.Orientation.Horizontal
                and role == Qt.ItemDataRole.DisplayRole):
            return _COLUMNS[section]
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        take = self._rows[index.row()]
        col = index.column()
        if col == _PROGRESS_COL:
            return self._progress_role(take, role)
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole):
            return self._text_cell(take, col)
        if role == Qt.ItemDataRole.ForegroundRole and col == _STATUS_COL:
            return _STATUS_DISPLAY.get(take.status, (None, None))[1]
        return None

    # ---- cell content ---------------------------------------------------
    def _text_cell(self, take, col: int) -> str:
        if col == _SHOT_COL:
            shot = self.project.get_shot(take.shot_id)
            return shot.name if shot else take.shot_id[:8]
        if col in (_MODEL_COL, _BACKEND_COL):
            model, backend = _model_backend(take)
            return model if col == _MODEL_COL else backend
        if col == _STATUS_COL:
            return _STATUS_DISPLAY.get(take.status, (take.status, None))[0]
        return ""

    def _progress_view(self, take) -> tuple[bool, int, str, str]:
        """(is_bar, pct, bar_label, text) for a take's Progress cell. A determinate bar is
        drawn only for a still-rendering *local* take (it reports a real per-step fraction
        over its WebSocket); a hosted take exposes no native %, so it - like a queued or
        finished take - shows plain text (latest line / "queued" / "done in X" / error)."""
        _, backend = _model_backend(take)
        if take.status == STATUS_GENERATING and backend == "comfyui":
            frac, lbl = self._latest_pct.get(take.id, (0.0, ""))
            pct = round(max(0.0, min(1.0, frac)) * 100)
            return True, pct, (lbl or f"{pct}%"), ""
        if take.status in (STATUS_GENERATING, STATUS_PENDING):
            text = self._latest.get(take.id, "")
            if not text and take.status == STATUS_PENDING:
                text = "queued"
            return False, 0, "", text
        if take.status == STATUS_FAILED and take.error:
            return False, 0, "", take.error
        if take.status == STATUS_DONE:
            elapsed = done_elapsed(take)
            return False, 0, "", (f"done in {elapsed}" if elapsed else "done")
        return False, 0, "", ""

    def _progress_role(self, take, role):
        is_bar, pct, bar_label, text = self._progress_view(take)
        if role == _BAR_ROLE:
            return pct if is_bar else None
        if role == _BAR_LABEL_ROLE:
            return bar_label
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole):
            return "" if is_bar else text
        return None

    # ---- mutation -------------------------------------------------------
    def take_at(self, row: int):
        return self._rows[row] if 0 <= row < len(self._rows) else None

    def rows(self) -> list:
        return self._rows

    def set_project(self, project: Project) -> None:
        self.project = project
        self._latest.clear()
        self._latest_pct.clear()
        self._dismissed.clear()

    def dismiss_finished(self) -> None:
        takes = self.project.list_takes()
        self._dismissed |= {t.id for t in takes if t.status in _FINISHED}

    def rebuild(self) -> list:
        """Recompute the ordered rows from the project. When the row identity+order is
        unchanged (a refresh() that didn't actually change the queue — e.g. switching to the
        tab) emit a granular dataChanged so scroll/selection survive; any add/remove/reorder
        (most status transitions reshuffle active-vs-finished) takes a full reset. Either way
        ZERO per-row widgets are created — that's the rule #18 fix. Returns the new row list.
        (Progress/% ticks don't come through here; they're single-cell updates, see
        update_line/update_pct.)"""
        takes = self.project.list_takes()
        finished_ids = {t.id for t in takes if t.status in _FINISHED}
        self._dismissed &= finished_ids                # drop ids of takes no longer present
        new_rows = select_rows(takes, self._dismissed)
        new_ids = [t.id for t in new_rows]
        old_ids = [t.id for t in self._rows]
        live = set(new_ids)                            # drop cached lines/pcts for evicted takes
        self._latest = {k: v for k, v in self._latest.items() if k in live}
        self._latest_pct = {k: v for k, v in self._latest_pct.items() if k in live}
        if new_ids == old_ids:
            self._rows = new_rows
            self._row_index = {t.id: i for i, t in enumerate(new_rows)}
            if new_rows:
                self.dataChanged.emit(self.index(0, 0),
                                      self.index(len(new_rows) - 1, len(_COLUMNS) - 1))
        else:
            self.beginResetModel()
            self._rows = new_rows
            self._row_index = {t.id: i for i, t in enumerate(new_rows)}
            self.endResetModel()
        return new_rows

    def update_line(self, take_id: str, line: str) -> None:
        self._latest[take_id] = line
        self._touch_progress(take_id)

    def update_pct(self, take_id: str, frac: float, label: str) -> None:
        self._latest_pct[take_id] = (frac, label)
        self._touch_progress(take_id)

    def _touch_progress(self, take_id: str) -> None:
        """Repaint just this take's Progress cell in place (no rebuild). If the take isn't
        currently shown, the cache is still updated so a later rebuild reflects it."""
        row = self._row_index.get(take_id)
        if row is not None:
            idx = self.index(row, _PROGRESS_COL)
            self.dataChanged.emit(idx, idx)


class _ProgressDelegate(QStyledItemDelegate):
    """Paints the Progress column. A determinate bar (QStyle CE_ProgressBar) is drawn when
    the model hands back a `_BAR_ROLE` percentage (a still-rendering local take); otherwise
    the cell falls back to the default text paint. No `QProgressBar` widget is created - this
    is the whole reason the Queue no longer accumulates widgets (rule #18)."""

    def paint(self, painter, option, index):  # noqa: N802 - Qt override
        pct = index.data(_BAR_ROLE)
        if pct is None:
            super().paint(painter, option, index)
            return
        if option.state & QStyle.StateFlag.State_Selected:
            painter.fillRect(option.rect, option.palette.highlight())
        opt = QStyleOptionProgressBar()
        opt.rect = option.rect.adjusted(3, 3, -3, -3)
        opt.minimum = 0
        opt.maximum = 100
        opt.progress = int(pct)
        opt.text = index.data(_BAR_LABEL_ROLE) or f"{int(pct)}%"
        opt.textVisible = True
        opt.textAlignment = Qt.AlignmentFlag.AlignCenter
        style = option.widget.style() if option.widget else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ProgressBar, opt, painter)


class QueueView(QWidget):
    def __init__(self, project: Project, jobs, parent=None, queue_actions=None):
        super().__init__(parent)
        self.project = project
        self.jobs = jobs
        self._queue_actions = list(queue_actions or [])   # QActions owned by MainWindow
        self.model = QueueModel(project, self)
        self._rebuild_count = 0                           # for smoke tests (coalescing)
        self._rebuild_timer = QTimer(self)                # coalesce a burst of signals -> 1 rebuild
        self._rebuild_timer.setSingleShot(True)
        self._rebuild_timer.setInterval(0)
        self._rebuild_timer.timeout.connect(self._do_rebuild)
        self._build()
        jobs.progress.connect(self._on_progress)
        jobs.progress_pct.connect(self._on_progress_pct)
        jobs.status_changed.connect(self._schedule_rebuild)
        jobs.finished.connect(self._schedule_rebuild)
        jobs.failed.connect(self._schedule_rebuild)
        self.refresh()

    def set_project(self, project: Project) -> None:
        """Point the queue view at a newly opened/created project (see MainWindow._switch_project)."""
        self.project = project
        self.model.set_project(project)
        self.refresh()

    # ---- build ----------------------------------------------------------
    def _build(self) -> None:
        self.summary = QLabel()
        self.summary.setStyleSheet("font-weight: 600; padding: 2px;")
        self.clear_btn = QPushButton("Clear finished")
        self.clear_btn.setToolTip(
            "Remove finished, failed and cancelled takes from this list "
            "(running and queued takes stay). Does not delete the takes themselves.")
        self.clear_btn.clicked.connect(self._clear_finished)
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setWordWrap(False)                  # single-line rows; full text in tooltips
        self.table.verticalHeader().setVisible(False)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._context_menu)
        self._progress_delegate = _ProgressDelegate(self.table)
        self.table.setItemDelegateForColumn(_PROGRESS_COL, self._progress_delegate)
        hh = self.table.horizontalHeader()
        for col in range(len(_COLUMNS)):
            mode = (QHeaderView.ResizeMode.Stretch if col == _PROGRESS_COL
                    else QHeaderView.ResizeMode.ResizeToContents)
            hh.setSectionResizeMode(col, mode)

        header = QHBoxLayout()
        for act in self._queue_actions:                # queue actions on the left (MainWindow-owned)
            btn = QToolButton()
            btn.setDefaultAction(act)                  # reflects the action's enabled/text state
            btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
            header.addWidget(btn)
        header.addWidget(self.summary)
        header.addStretch(1)
        header.addWidget(self.clear_btn)

        lay = QVBoxLayout(self)
        lay.addLayout(header)
        lay.addWidget(self.table, 1)

    # ---- rebuild (coalesced) -------------------------------------------
    def _schedule_rebuild(self, *_args) -> None:
        """Coalesce a burst of structural signals (status_changed / finished / failed, incl.
        a per-take mass-cancel storm) into a single rebuild on the next event-loop turn."""
        self._rebuild_timer.start()

    def refresh(self) -> None:
        """Rebuild now, synchronously. External callers (MainWindow, showEvent) use this so
        the queue is current immediately after an action that emits no signal yet."""
        self._do_rebuild()

    def _do_rebuild(self) -> None:
        self._rebuild_timer.stop()
        self._rebuild_count += 1
        rows = self.model.rebuild()
        self._update_summary(rows)

    def _update_summary(self, rows: list) -> None:
        takes = self.project.list_takes()
        n_run = sum(1 for t in takes if t.status == STATUS_GENERATING)
        n_queue = sum(1 for t in takes if t.status == STATUS_PENDING)
        n_fail = sum(1 for t in takes if t.status == STATUS_FAILED)
        summary = f"{n_run} running · {n_queue} queued"
        if n_fail:
            summary += f" · {n_fail} failed"
        if not rows:
            summary = "Queue empty - nothing generating or queued."
        self.summary.setText(summary)
        # enable Clear only when a finished row is actually showing to be cleared
        self.clear_btn.setEnabled(any(t.status in _FINISHED for t in rows))

    # ---- events ---------------------------------------------------------
    def showEvent(self, event):  # noqa: N802 - Qt override
        super().showEvent(event)
        self.refresh()           # always current when the tab is brought to the front

    def _on_progress(self, take_id: str, line: str) -> None:
        self.model.update_line(take_id, line)

    def _on_progress_pct(self, take_id: str, frac: float, label: str) -> None:
        self.model.update_pct(take_id, frac, label)

    # ---- per-take cancel (right-click) ---------------------------------
    def _context_menu(self, pos) -> None:
        ids = self._selected_take_ids()
        idx = self.table.indexAt(pos)
        if idx.isValid():
            clicked = self.model.take_at(idx.row())
            if clicked and clicked.id not in ids:
                ids = [clicked.id]                     # right-click outside the selection acts on that row
        menu = self._build_context_menu(ids)
        if menu.actions():
            menu.exec(self.table.viewport().mapToGlobal(pos))

    def _selected_take_ids(self) -> list:
        out = []
        for i in self.table.selectionModel().selectedRows():
            t = self.model.take_at(i.row())
            if t:
                out.append(t.id)
        return out

    def _build_context_menu(self, take_ids: list) -> QMenu:
        """The queued-take right-click menu, built without exec() so it's headless-testable
        (the takes_view / shot_card pattern). Only still-PENDING takes can be cancelled here;
        running takes are stopped from the ComfyUI tab and finished ones have nothing to cancel."""
        menu = QMenu(self)
        pending = [tid for tid in take_ids
                   if (t := self.project.get_take(tid)) and t.status == STATUS_PENDING]
        if pending:
            label = ("Cancel queued generation" if len(pending) == 1
                     else f"Cancel {len(pending)} queued generations")
            menu.addAction(label).triggered.connect(lambda: self._cancel(pending))
        return menu

    def _cancel(self, take_ids: list) -> None:
        for tid in take_ids:
            self.jobs.cancel_take(tid)
        self.refresh()

    def _clear_finished(self) -> None:
        """Hide every finished/failed/cancelled take from the list, keeping active ones.

        UI-only: the takes themselves are untouched (a done take stays in the project and
        its triage view) — this just dismisses them from the live queue monitor. Takes that
        finish *after* this aren't dismissed, so the list keeps reflecting new results."""
        self.model.dismiss_finished()
        self.refresh()
