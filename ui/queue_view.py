"""Queue tab - a live view of every generation in flight.

The generation queue (backends/jobs.JobManager) runs hosted jobs a few in parallel and
local jobs one at a time, streaming free-text progress lines into the main log. That log
interleaves every job's output keyed only by an 8-char take-id, so it's hard to see *what's
running, what's queued behind it, and how far along each is*. This tab assembles that into a
list: one row per active (queued / generating) take plus the most-recently finished ones,
each showing its shot, model, backend, status, latest progress line, and - for a still-queued
take - a per-row Cancel.

It owns no real state of its own beyond a cache of each take's latest progress line. Rows are
derived from project.list_takes() by status and refreshed from the JobManager's existing
signals (progress / status_changed / finished / failed) and whenever the tab is shown - the
queue engine is untouched. Progress is free text (backends emit log lines, not a percentage),
so the Progress column shows the latest line rather than a bar.
"""
from __future__ import annotations

from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QHeaderView, QLabel, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
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

# status value -> (label shown in the Status column, row tint)
_STATUS_DISPLAY = {
    STATUS_GENERATING: ("running", QColor("#1a7f37")),
    STATUS_PENDING:    ("queued",  QColor("#57606a")),
    STATUS_DONE:       ("done",    QColor("#1a7f37")),
    STATUS_FAILED:     ("failed",  QColor("#cf222e")),
    STATUS_CANCELLED:  ("cancelled", QColor("#57606a")),
}


class QueueView(QWidget):
    def __init__(self, project: Project, jobs, parent=None):
        super().__init__(parent)
        self.project = project
        self.jobs = jobs
        self._latest: dict[str, str] = {}              # take_id -> last progress line
        self._progress_items: dict[str, QTableWidgetItem] = {}  # take_id -> its Progress cell
        self._build()
        jobs.progress.connect(self._on_progress)
        jobs.status_changed.connect(lambda *_: self.refresh())
        jobs.finished.connect(lambda *_: self.refresh())
        jobs.failed.connect(lambda *_: self.refresh())
        self.refresh()

    def set_project(self, project: Project) -> None:
        """Point the queue view at a newly opened/created project (see MainWindow._switch_project)."""
        self.project = project
        self._latest.clear()
        self.refresh()

    # ---- build ----------------------------------------------------------
    def _build(self) -> None:
        self.summary = QLabel()
        self.summary.setStyleSheet("font-weight: 600; padding: 2px;")
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

        lay = QVBoxLayout(self)
        lay.addWidget(self.summary)
        lay.addWidget(self.table, 1)

    # ---- data -----------------------------------------------------------
    def _rows(self) -> list:
        """The takes to show: every active (generating/pending) take first, in queue order,
        then the most-recently finished ones (so results stay visible without unbounded growth)."""
        takes = self.project.list_takes()
        active = [t for t in takes if t.status == STATUS_GENERATING]
        active += [t for t in takes if t.status == STATUS_PENDING]
        finished = [t for t in takes
                    if t.status in (STATUS_DONE, STATUS_FAILED, STATUS_CANCELLED)]
        return active + finished[-_RECENT_LIMIT:][::-1]   # newest finished first

    def _model_backend(self, take) -> tuple[str, str]:
        snap = take.settings_snapshot or {}
        model_id = snap.get("model_id", "")
        m = library.get_model(model_id)
        name = m["display_name"] if m else (model_id or "?")
        return name, snap.get("backend", "") or (m["backend"] if m else "")

    def _progress_text(self, take) -> str:
        if take.status == STATUS_FAILED and take.error:
            return take.error
        if take.status in (STATUS_GENERATING, STATUS_PENDING):
            return self._latest.get(take.id, "")
        return ""

    def refresh(self) -> None:
        rows = self._rows()
        n_run = sum(1 for t in self.project.list_takes() if t.status == STATUS_GENERATING)
        n_queue = sum(1 for t in self.project.list_takes() if t.status == STATUS_PENDING)
        n_fail = sum(1 for t in self.project.list_takes() if t.status == STATUS_FAILED)
        summary = f"{n_run} running · {n_queue} queued"
        if n_fail:
            summary += f" · {n_fail} failed"
        if not rows:
            summary = "Queue empty - nothing generating or queued."
        self.summary.setText(summary)

        self._progress_items.clear()
        self.table.setRowCount(len(rows))
        for row, take in enumerate(rows):
            shot = self.project.get_shot(take.shot_id)
            model, backend = self._model_backend(take)
            label, tint = _STATUS_DISPLAY.get(take.status, (take.status, None))
            prog = self._progress_text(take)
            cells = [shot.name if shot else take.shot_id[:8], model, backend, label, prog]
            for col, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setToolTip(text)
                if col == 3 and tint is not None:
                    item.setForeground(tint)
                self.table.setItem(row, col, item)
                if col == _PROGRESS_COL:
                    self._progress_items[take.id] = item
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
        item = self._progress_items.get(take_id)      # update just this row, no full rebuild
        if item is not None:
            item.setText(line)
            item.setToolTip(line)

    def _cancel(self, take_id: str) -> None:
        self.jobs.cancel_take(take_id)
        self.refresh()
