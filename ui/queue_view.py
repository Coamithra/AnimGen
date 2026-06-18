"""Queue tab - a live view of every generation in flight.

The generation queue (backends/jobs.JobManager) runs hosted jobs a few in parallel and
local jobs one at a time, streaming free-text progress lines into the main log. That log
interleaves every job's output keyed only by an 8-char take-id, so it's hard to see *what's
running, what's queued behind it, and how far along each is*. This tab assembles that into a
list: one row per active (queued / generating) take plus the most-recently finished ones,
each showing its shot, model, backend, status, latest progress line, and - for a still-queued
take - a per-row Cancel.

It owns no real state of its own beyond a cache of each take's latest progress line and
fraction. Rows are derived from project.list_takes() by status and refreshed from the
JobManager's signals (progress / progress_pct / status_changed / finished / failed) and
whenever the tab is shown - the queue engine is untouched. The Progress column shows a
QProgressBar: a determinate % bar for a local (ComfyUI) render, which reports real per-step
progress over its WebSocket via the progress_pct signal, and an indeterminate "busy" bar
labelled with the latest line (e.g. elapsed time) for hosted (Replicate) takes, which expose
no native percentage. Failed takes show their error as text instead.
"""
from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QHBoxLayout, QHeaderView, QLabel, QProgressBar, QPushButton,
    QTableWidget, QTableWidgetItem, QToolButton, QVBoxLayout, QWidget,
)

import library
from store.project import Project
from store.models import (
    STATUS_CANCELLED, STATUS_DONE, STATUS_FAILED, STATUS_GENERATING, STATUS_PENDING,
)

_COLUMNS = ["Shot", "Model", "Backend", "Status", "Progress", ""]
_PROGRESS_COL = _COLUMNS.index("Progress")
_CANCEL_COL = len(_COLUMNS) - 1
_RECENT_LIMIT = 15      # how many finished takes to keep visible below the active ones
_FINISHED = (STATUS_DONE, STATUS_FAILED, STATUS_CANCELLED)

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


class QueueView(QWidget):
    def __init__(self, project: Project, jobs, parent=None, queue_actions=None):
        super().__init__(parent)
        self.project = project
        self.jobs = jobs
        self._queue_actions = list(queue_actions or [])   # QActions owned by MainWindow
        self._latest: dict[str, str] = {}              # take_id -> last progress line
        self._latest_pct: dict[str, tuple[float, str]] = {}     # take_id -> (fraction, label)
        self._progress_items: dict[str, QTableWidgetItem] = {}  # take_id -> its Progress text cell
        self._progress_bars: dict[str, QProgressBar] = {}       # take_id -> its Progress bar
        self._dismissed: set[str] = set()              # finished take_ids hidden via Clear
        self._build()
        jobs.progress.connect(self._on_progress)
        jobs.progress_pct.connect(self._on_progress_pct)
        jobs.status_changed.connect(lambda *_: self.refresh())
        jobs.finished.connect(lambda *_: self.refresh())
        jobs.failed.connect(lambda *_: self.refresh())
        self.refresh()

    def set_project(self, project: Project) -> None:
        """Point the queue view at a newly opened/created project (see MainWindow._switch_project)."""
        self.project = project
        self._latest.clear()
        self._latest_pct.clear()
        self._dismissed.clear()
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
        self.table = QTableWidget(0, len(_COLUMNS))
        self.table.setHorizontalHeaderLabels(_COLUMNS)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setWordWrap(False)                  # single-line rows; full text in tooltips
        self.table.verticalHeader().setVisible(False)
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

    # ---- data -----------------------------------------------------------
    def _rows(self) -> list:
        return select_rows(self.project.list_takes(), self._dismissed)

    def _model_backend(self, take) -> tuple[str, str]:
        snap = take.settings_snapshot or {}
        model_id = snap.get("model_id", "")
        m = library.get_model(model_id)
        name = m["display_name"] if m else (model_id or "?")
        return name, snap.get("backend", "") or (m["backend"] if m else "")

    def _set_progress_cell(self, row: int, take, backend: str) -> None:
        """Fill the Progress column for one row: a determinate % bar for an active local
        (ComfyUI) render, an indeterminate busy bar labelled with the latest line for an
        active hosted take (or anything still queued), or plain text otherwise (errors)."""
        if take.status in (STATUS_GENERATING, STATUS_PENDING):
            bar = QProgressBar()
            bar.setTextVisible(True)
            if backend == "comfyui" and take.status == STATUS_GENERATING:
                frac, lbl = self._latest_pct.get(take.id, (0.0, ""))
                bar.setRange(0, 100)
                bar.setValue(round(frac * 100))
                bar.setFormat(lbl or "%p%")
            else:                                     # hosted (no native %), or still queued
                bar.setRange(0, 0)                    # busy / indeterminate
                txt = self._latest.get(take.id, "")
                bar.setFormat(txt)
                bar.setToolTip(txt)
            self.table.setItem(row, _PROGRESS_COL, QTableWidgetItem())   # clear stale text
            self.table.setCellWidget(row, _PROGRESS_COL, bar)
            self._progress_bars[take.id] = bar
            return
        self.table.removeCellWidget(row, _PROGRESS_COL)                  # finished/failed: text
        if take.status == STATUS_FAILED and take.error:
            text = take.error
        elif take.status == STATUS_DONE:
            elapsed = done_elapsed(take)
            text = f"done in {elapsed}" if elapsed else "done"
        else:
            text = ""
        item = QTableWidgetItem(text)
        item.setToolTip(text)
        self.table.setItem(row, _PROGRESS_COL, item)
        self._progress_items[take.id] = item

    def refresh(self) -> None:
        takes = self.project.list_takes()
        finished_ids = {t.id for t in takes if t.status in _FINISHED}
        self._dismissed &= finished_ids            # drop ids of takes no longer present
        rows = self._rows()
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

        self._progress_items.clear()
        self._progress_bars.clear()
        live = {t.id for t in rows}                 # drop cached lines/pcts for evicted takes
        self._latest = {k: v for k, v in self._latest.items() if k in live}
        self._latest_pct = {k: v for k, v in self._latest_pct.items() if k in live}
        self.table.setRowCount(len(rows))
        for row, take in enumerate(rows):
            shot = self.project.get_shot(take.shot_id)
            model, backend = self._model_backend(take)
            label, tint = _STATUS_DISPLAY.get(take.status, (take.status, None))
            cells = [shot.name if shot else take.shot_id[:8], model, backend, label]
            for col, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setToolTip(text)
                if col == 3 and tint is not None:
                    item.setForeground(tint)
                self.table.setItem(row, col, item)
            self._set_progress_cell(row, take, backend)
            if take.status == STATUS_PENDING:
                btn = QPushButton("Cancel")
                btn.setToolTip("Remove this queued generation before it starts")
                btn.clicked.connect(lambda _checked=False, tid=take.id: self._cancel(tid))
                self.table.setCellWidget(row, _CANCEL_COL, btn)
            else:
                self.table.removeCellWidget(row, _CANCEL_COL)

    # ---- events ---------------------------------------------------------
    def showEvent(self, event):  # noqa: N802 - Qt override
        super().showEvent(event)
        self.refresh()           # always current when the tab is brought to the front

    def _on_progress(self, take_id: str, line: str) -> None:
        self._latest[take_id] = line
        bar = self._progress_bars.get(take_id)         # busy (hosted/queued) bar: relabel it
        if bar is not None and bar.maximum() == 0:
            bar.setFormat(line)
            bar.setToolTip(line)
        item = self._progress_items.get(take_id)       # failed-text cell, if any
        if item is not None:
            item.setText(line)
            item.setToolTip(line)

    def _on_progress_pct(self, take_id: str, frac: float, label: str) -> None:
        self._latest_pct[take_id] = (frac, label)
        bar = self._progress_bars.get(take_id)         # update just this row, no full rebuild
        if bar is not None:
            if bar.maximum() == 0:                     # first fraction: flip busy -> determinate
                bar.setRange(0, 100)
            bar.setValue(round(frac * 100))
            bar.setFormat(label or "%p%")

    def _cancel(self, take_id: str) -> None:
        self.jobs.cancel_take(take_id)
        self.refresh()

    def _clear_finished(self) -> None:
        """Hide every finished/failed/cancelled take from the list, keeping active ones.

        UI-only: the takes themselves are untouched (a done take stays in the project and
        its triage view) — this just dismisses them from the live queue monitor. Takes that
        finish *after* this aren't dismissed, so the list keeps reflecting new results."""
        takes = self.project.list_takes()
        self._dismissed |= {t.id for t in takes if t.status in _FINISHED}
        self.refresh()
