"""Model Library window - a view of model_library.json plus live-schema fetching.

Lists every available model with backend, cost, capabilities and notes. The roster itself
is authored in model_library.json and not user-editable (per spec), but per-parameter
input schemas are fetched LIVE from Replicate. The **Fetch live schemas** button pulls the
current input schema for every Replicate model and caches it (store.schema_cache); shot
editors then reuse those cached schemas (correct enums/types) instead of each re-fetching.
The Schema column shows the cached field count per model.
"""
from __future__ import annotations

import threading

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QHBoxLayout, QHeaderView, QLabel, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

import library
from store import schema_cache

_COLUMNS = ["Model", "Backend", "Cost", "End frame", "Data-URI", "Duration", "Schema", "Notes"]
_SCHEMA_COL = _COLUMNS.index("Schema")
_NOTES_COL = _COLUMNS.index("Notes")


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


def _schema_cell(m: dict) -> str:
    """Cached-schema state for the Schema column: field count, 'not fetched', or 'n/a'."""
    if m.get("backend") != "replicate":
        return "n/a"                               # local backends have no Replicate schema
    e = schema_cache.entry(m.get("replicate_model_id"))
    return f"{e['fields']} fields" if e else "not fetched"


class _SchemaFetcher(QObject):
    """Fetches every Replicate model's input schema off the GUI thread, caching each.

    One daemon thread walks the model list; results are emitted back as queued signals so
    the table updates on the GUI thread (mirrors the ComfyUI tab's off-thread callers).
    """
    result = Signal(str, int, str)   # replicate_model_id, field_count (-1 = failed), error
    finished = Signal(int, int)      # ok_count, fail_count

    def __init__(self, replicate_ids: list[str]):
        super().__init__()
        self._ids = replicate_ids

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        from backends import replicate_client
        try:
            token = replicate_client.load_token()
        except Exception as e:  # noqa: BLE001 - no token -> the whole batch fails the same way
            for rid in self._ids:
                self.result.emit(rid, -1, str(e))
            self.finished.emit(0, len(self._ids))
            return
        ok = fail = 0
        for rid in self._ids:
            try:
                props, _ = replicate_client.get_input_schema(token, rid)
                schema_cache.put(rid, props)
                ok += 1
                self.result.emit(rid, len(props), "")
            except Exception as e:  # noqa: BLE001 - report per-model and keep going
                fail += 1
                self.result.emit(rid, -1, str(e))
        self.finished.emit(ok, fail)


class ModelLibraryWindow(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Model Library")
        self.resize(960, 480)
        self.models = library.models()
        self._row_by_rid: dict[str, int] = {}      # replicate_model_id -> table row
        self._fetcher: _SchemaFetcher | None = None
        self._build()

    def _build(self) -> None:
        self.table = QTableWidget(len(self.models), len(_COLUMNS))
        self.table.setHorizontalHeaderLabels(_COLUMNS)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.verticalHeader().setVisible(False)
        self.table.setWordWrap(True)

        for row, m in enumerate(self.models):
            cells = [
                m["display_name"], m["backend"], _cost(m),
                "yes" if m.get("supports_end_frame") else "no",
                "required" if m.get("requires_data_uri") else "-",
                _duration(m), _schema_cell(m), m.get("notes", ""),
            ]
            for col, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if col == _NOTES_COL:
                    item.setToolTip(text)
                self.table.setItem(row, col, item)
            rid = m.get("replicate_model_id")
            if m.get("backend") == "replicate" and rid:
                self._row_by_rid[rid] = row

        hh = self.table.horizontalHeader()
        for col in range(len(_COLUMNS) - 1):
            hh.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(_NOTES_COL, QHeaderView.ResizeMode.Stretch)
        self.table.resizeRowsToContents()

        self.fetch_btn = QPushButton("Fetch live schemas")
        self.fetch_btn.setToolTip("Fetch every Replicate model's current input schema and cache "
                                  "it for the shot editor (no spend - schema read only).")
        self.fetch_btn.clicked.connect(self._fetch_all)
        self.status = QLabel("")
        self.status.setStyleSheet("color: gray;")
        actions = QHBoxLayout()
        actions.addWidget(self.fetch_btn)
        actions.addWidget(self.status, 1)

        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("Available models (roster read-only - edit model_library.json to change):"))
        lay.addLayout(actions)
        lay.addWidget(self.table)

    # ---- live-schema fetch ----------------------------------------------
    def _fetch_all(self) -> None:
        rids = list(self._row_by_rid)
        if not rids:
            return
        self.fetch_btn.setEnabled(False)
        self.status.setText(f"Fetching {len(rids)} schema(s)…")
        self._fetcher = _SchemaFetcher(rids)       # kept on self so it isn't GC'd mid-fetch
        self._fetcher.result.connect(self._on_result)
        self._fetcher.finished.connect(self._on_finished)
        self._fetcher.start()

    def _on_result(self, replicate_model_id: str, fields: int, error: str) -> None:
        row = self._row_by_rid.get(replicate_model_id)
        if row is None:
            return
        item = self.table.item(row, _SCHEMA_COL)
        if item is None:
            return
        item.setText(f"{fields} fields" if fields >= 0 else "fetch failed")
        item.setToolTip(error)
        self.table.resizeRowsToContents()

    def _on_finished(self, ok: int, fail: int) -> None:
        self.fetch_btn.setEnabled(True)
        msg = f"Cached {ok} schema(s)" + (f", {fail} failed" if fail else "")
        self.status.setText(msg)
