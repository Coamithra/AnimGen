"""Headless probe: does QTableWidget cell-widget churn (the queue_view.refresh pattern)
leak child widgets into the table viewport? If the live QProgressBar/QPushButton count
climbs across refreshes (after draining deleteLater), that's the paintSiblingsRecursive
sibling source.  Run: QT_QPA_PLATFORM=offscreen .venv/Scripts/python.exe scripts/_leak_probe.py
"""
import os, sys
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent
from PySide6.QtWidgets import (
    QApplication, QTableWidget, QTableWidgetItem, QProgressBar, QPushButton,
)

app = QApplication([])
MODE = sys.argv[1] if len(sys.argv) > 1 else "leak"   # "leak" | "fix"

COLS = 6
PROGRESS_COL = 4
CANCEL_COL = 5
table = QTableWidget(0, COLS)


def discard_cell_widgets():
    """The fix: explicitly destroy old cell widgets before re-laying the table."""
    for r in range(table.rowCount()):
        for c in (PROGRESS_COL, CANCEL_COL):
            w = table.cellWidget(r, c)
            if w is not None:
                table.removeCellWidget(r, c)
                w.setParent(None)
                w.deleteLater()


def live_counts():
    bars = sum(1 for w in app.allWidgets() if isinstance(w, QProgressBar))
    btns = sum(1 for w in app.allWidgets() if isinstance(w, QPushButton))
    vp_children = len(table.viewport().children())
    return bars, btns, vp_children


def set_progress_cell(row, status):
    # mirrors queue_view._set_progress_cell
    if status in ("generating", "pending"):
        bar = QProgressBar()
        bar.setTextVisible(True)
        if status == "generating":
            bar.setRange(0, 100); bar.setValue(50); bar.setFormat("50%")
        else:
            bar.setRange(0, 0); bar.setFormat("queued")
        table.setItem(row, PROGRESS_COL, QTableWidgetItem())
        table.setCellWidget(row, PROGRESS_COL, bar)
        return
    table.removeCellWidget(row, PROGRESS_COL)
    table.setItem(row, PROGRESS_COL, QTableWidgetItem("done in 3m"))


def refresh(rows):
    # rows: list[str status]; mirrors queue_view.refresh
    table.setRowCount(len(rows))
    for row, status in enumerate(rows):
        for col in range(PROGRESS_COL):
            table.setItem(row, col, QTableWidgetItem(f"r{row}c{col}"))
        set_progress_cell(row, status)
        if status == "pending":
            btn = QPushButton("Cancel")
            table.setCellWidget(row, CANCEL_COL, btn)
        else:
            table.removeCellWidget(row, CANCEL_COL)


# Simulate a batch: 50 pending draining to generating->done, rows churn every refresh.
N = 600
for i in range(N):
    # a moving window: some pending, one generating, a few finished
    n_pending = max(0, 50 - i // 12)
    rows = ["generating"] + ["pending"] * n_pending + ["done"] * 15
    if MODE == "fix":
        discard_cell_widgets()
    refresh(rows)
    app.processEvents()              # process normal posted events
    app.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)   # actually drain deleteLater
    if i % 50 == 0 or i == N - 1:
        bars, btns, vpc = live_counts()
        print(f"refresh {i:4d}  rows={len(rows):3d}  live QProgressBar={bars:5d}  "
              f"QPushButton={btns:5d}  viewport_children={vpc:5d}")
