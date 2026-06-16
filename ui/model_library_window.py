"""Model Library window - a separate, read-only view of model_library.json.

Lists every available model with backend, cost, capabilities and notes. Not user
editable (per spec) - the roster is authored in model_library.json.
"""
from __future__ import annotations

from PySide6.QtWidgets import (
    QAbstractItemView, QHeaderView, QLabel, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

import library

_COLUMNS = ["Model", "Backend", "Cost", "End frame", "Data-URI", "Duration", "Notes"]


def _cost(m: dict) -> str:
    c = m.get("cost_per_second_usd")
    if isinstance(c, dict):                       # per-resolution table -> show the span
        vals = [v for v in c.values() if v is not None]
        if not vals:
            return "?"
        lo, hi = min(vals), max(vals)
        return f"${lo:.3f}/s" if lo == hi else f"${lo:.3f}-${hi:.3f}/s"
    if c == 0:
        return "free"
    return f"${c:.3f}/s" if c is not None else "?"


def _duration(m: dict) -> str:
    dr = m.get("duration_range")
    if dr:
        return f"{dr[0]}-{dr[1]}s"
    return f"{m['frames']}f" if m.get("frames") else "-"


class ModelLibraryWindow(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Model Library")
        self.resize(960, 480)
        models = library.models()

        table = QTableWidget(len(models), len(_COLUMNS))
        table.setHorizontalHeaderLabels(_COLUMNS)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.verticalHeader().setVisible(False)
        table.setWordWrap(True)

        for row, m in enumerate(models):
            cells = [
                m["display_name"], m["backend"], _cost(m),
                "yes" if m.get("supports_end_frame") else "no",
                "required" if m.get("requires_data_uri") else "-",
                _duration(m), m.get("notes", ""),
            ]
            for col, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if col == 6:
                    item.setToolTip(text)
                table.setItem(row, col, item)

        hh = table.horizontalHeader()
        for col in range(len(_COLUMNS) - 1):
            hh.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(len(_COLUMNS) - 1, QHeaderView.ResizeMode.Stretch)
        table.resizeRowsToContents()

        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("Available models (read-only - edit model_library.json to change):"))
        lay.addWidget(table)
