"""Model Library window - a view of model_library.json plus live-schema fetching.

Lists every available model with backend, cost, capabilities and notes. The roster's
canonical metadata (IDs, costs, notes, aspect_ratios) is authored in model_library.json,
but per-parameter input schemas are fetched LIVE from Replicate. The **Refresh from
Replicate** button pulls the current input schema for every Replicate model, caches it
(store.schema_cache), AND derives each model's capability flags (negative prompt / fixed
camera) from that schema and writes them back into model_library.json
(library.sync_model_capabilities). Shot editors reuse the cached schemas (correct
enums/types). The Schema column shows the cached field count; the Capabilities column
shows the synced flags. (Pricing is NOT exposed by Replicate's API, so costs stay
authored - the refresh deliberately leaves them alone.)
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

_COLUMNS = ["Model", "Backend", "Cost", "End frame", "Capabilities", "Data-URI",
            "Duration", "Schema", "Notes"]
_CAPS_COL = _COLUMNS.index("Capabilities")
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


class _WrapTable(QTableWidget):
    """A table whose word-wrapped rows recompute their height once the columns settle.

    The Notes column is Stretch, so its real width is only known after the table has
    been laid out. If we size rows before that (while Notes is still at its default
    narrow width), each long note wraps into a tall, skinny column and the row balloons
    to fill the viewport — and Qt never shrinks it back once the column widens. Sizing
    rows from resizeEvent (after super() has applied the stretch) keeps heights correct
    on first show and on every window resize. The guard stops the row-height change from
    re-entering via the scrollbar appearing/disappearing."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._sizing = False

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._sizing:
            return
        self._sizing = True
        try:
            self.resizeRowsToContents()
        finally:
            self._sizing = False


def _caps_tags(caps: dict) -> str:
    """Render the synced capability flags as short tags for the Capabilities column (end
    frame has its own column, so it's omitted here). Shared by the initial table build and
    the live refresh update so the tag vocabulary stays in one place."""
    tags = []
    if caps.get("supports_negative_prompt"):
        tags.append("negative")
    if caps.get("supports_camera_fixed"):
        tags.append("camera-fixed")
    return ", ".join(tags) if tags else "-"


class _ReplicateRefresher(QObject):
    """Refreshes every Replicate model off the GUI thread: fetches its input schema (caching
    each), derives capability flags from that schema, and syncs the flags into
    model_library.json.

    One daemon thread walks the model list; results are emitted back as queued signals so
    the table updates on the GUI thread (mirrors the ComfyUI tab's off-thread callers).
    """
    # replicate_model_id, field_count (-1 = failed), error, capabilities dict (or None)
    result = Signal(str, int, str, object)
    finished = Signal(int, int, int)   # ok_count, fail_count, capability_change_count

    def __init__(self, models: list[dict]):
        super().__init__()
        self._models = models          # replicate models only; need both id and replicate id

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        from backends import replicate_client
        try:
            token = replicate_client.load_token()
        except Exception as e:  # noqa: BLE001 - no token -> the whole batch fails the same way
            for m in self._models:
                self.result.emit(m["replicate_model_id"], -1, str(e), None)
            self.finished.emit(0, len(self._models), 0)
            return
        ok = fail = changed = 0
        for m in self._models:
            rid = m["replicate_model_id"]
            try:
                props, _ = replicate_client.get_input_schema(token, rid)
                schema_cache.put(rid, props)
                caps = replicate_client.derive_capabilities(props)
                diff = library.sync_model_capabilities(m["id"], caps)
                if diff:
                    changed += 1
                ok += 1
                self.result.emit(rid, len(props), "", caps)
            except Exception as e:  # noqa: BLE001 - report per-model and keep going
                fail += 1
                self.result.emit(rid, -1, str(e), None)
        self.finished.emit(ok, fail, changed)


class ModelLibraryWindow(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Model Library")
        self.resize(960, 480)
        self.models = library.models()
        self._row_by_rid: dict[str, int] = {}      # replicate_model_id -> table row
        self._refresher: _ReplicateRefresher | None = None
        self._build()

    def _build(self) -> None:
        self.table = _WrapTable(len(self.models), len(_COLUMNS))
        self.table.setHorizontalHeaderLabels(_COLUMNS)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.verticalHeader().setVisible(False)
        self.table.setWordWrap(True)

        for row, m in enumerate(self.models):
            cells = [
                m["display_name"], m["backend"], _cost(m),
                "yes" if m.get("supports_end_frame") else "no", _caps_tags(m),
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

        self.refresh_btn = QPushButton("Refresh from Replicate")
        self.refresh_btn.setToolTip("Fetch every Replicate model's current input schema (cached "
                                    "for the shot editor) and sync its capability flags "
                                    "(negative prompt / fixed camera) into model_library.json. "
                                    "No spend - schema read only. Pricing isn't exposed by the "
                                    "API, so costs are left alone.")
        self.refresh_btn.clicked.connect(self.start_schema_fetch)
        self.status = QLabel("")
        self.status.setStyleSheet("color: gray;")
        actions = QHBoxLayout()
        actions.addWidget(self.refresh_btn)
        actions.addWidget(self.status, 1)

        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("Available models (costs/notes authored in model_library.json; "
                             "capabilities synced by Refresh from Replicate):"))
        lay.addLayout(actions)
        lay.addWidget(self.table)

    # ---- refresh from Replicate (schema cache + capability sync) --------
    def start_schema_fetch(self) -> None:
        """Public entry to kick off the off-thread refresh — used both by the Refresh
        button and by MainWindow's 'update model data on startup' setting. Caches each
        model's input schema and syncs its capability flags into model_library.json."""
        self._refresh_all()

    def _refresh_all(self) -> None:
        rep_models = [m for m in self.models
                      if m.get("backend") == "replicate" and m.get("replicate_model_id")]
        if not rep_models:
            return
        self.refresh_btn.setEnabled(False)
        self.status.setText(f"Refreshing {len(rep_models)} model(s)…")
        self._refresher = _ReplicateRefresher(rep_models)  # kept on self so it isn't GC'd mid-run
        self._refresher.result.connect(self._on_result)
        self._refresher.finished.connect(self._on_finished)
        self._refresher.start()

    def _on_result(self, replicate_model_id: str, fields: int, error: str, caps) -> None:
        row = self._row_by_rid.get(replicate_model_id)
        if row is None:
            return
        item = self.table.item(row, _SCHEMA_COL)
        if item is not None:
            item.setText(f"{fields} fields" if fields >= 0 else "fetch failed")
            item.setToolTip(error)
        if caps:                                   # refresh the live-derived capability cell
            caps_item = self.table.item(row, _CAPS_COL)
            if caps_item is not None:
                caps_item.setText(_caps_tags(caps))
        self.table.resizeRowsToContents()

    def _on_finished(self, ok: int, fail: int, changed: int) -> None:
        self.refresh_btn.setEnabled(True)
        msg = f"Cached {ok} schema(s)"
        msg += f", synced capabilities ({changed} changed)" if ok else ""
        if fail:
            msg += f", {fail} failed"
        self.status.setText(msg)
