"""Main window.

Shows animation configs as expandable cards (header + inline results folder view),
with global filters (model, starred). Generate resolves a config's model + params,
runs the cost-confirm gate, creates a pending result with an immutable
settings_snapshot, and enqueues a background job whose status streams into the log
panel and refreshes the originating card. Export is wired in Phase 5.
"""
from __future__ import annotations

import threading
import time
from typing import Optional

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFileDialog, QLabel, QMainWindow, QMessageBox,
    QPlainTextEdit, QScrollArea, QSplitter, QToolBar, QVBoxLayout, QWidget,
)

import library
import paths
from backends import comfy_client, replicate_client
from backends.jobs import JobManager
from pipeline import export
from store.db import Store
from store.models import STATUS_PENDING
from ui.config_card import ConfigCard
from ui.comfy_monitor_window import ComfyMonitorWindow
from ui.config_editor import ConfigEditor
from ui.cost_confirm import confirm_launch
from ui.model_library_window import ModelLibraryWindow

# settings keys passed to the hosted client explicitly (everything else -> extra/--set)
_EXPLICIT_SETTINGS = ("duration", "resolution", "seed", "length")


class _ComfyController(QObject):
    """Drives the Launch-ComfyUI flow entirely off the GUI thread.

    Probing a down server costs a full socket timeout on Windows (a closed localhost
    port drops SYNs rather than refusing), so even the initial "is it up?" check would
    freeze the GUI. The whole probe -> launch -> poll sequence therefore runs on a daemon
    thread and reports back via `result`, whose queued delivery hops to the GUI thread.
    Outcomes: 'running', 'launching', 'ready', 'timeout', 'failed' (info is a status
    dict, or {'message': ...} for 'failed').
    """
    result = Signal(str, object)

    def __init__(self, attempts: int = 45, interval: float = 2.0):
        super().__init__()
        self._attempts = attempts
        self._interval = interval

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        st = comfy_client.server_status(timeout=2)
        if st["running"]:
            self.result.emit("running", st)
            return
        try:
            comfy_client.launch_server()
        except comfy_client.ComfyError as e:
            self.result.emit("failed", {"message": str(e)})
            return
        self.result.emit("launching", {})
        for _ in range(self._attempts):
            time.sleep(self._interval)
            st = comfy_client.server_status(timeout=2)
            if st["running"]:
                self.result.emit("ready", st)
                return
        self.result.emit("timeout", {})


class MainWindow(QMainWindow):
    def __init__(self, store: Store):
        super().__init__()
        self.store = store
        self.cards: dict[str, ConfigCard] = {}

        self.jobs = JobManager(store)
        self.jobs.progress.connect(self._on_progress)
        self.jobs.status_changed.connect(self._on_status_changed)
        self.jobs.finished.connect(lambda rid: self._after_job(rid, f"✓ done {rid[:8]}"))
        self.jobs.failed.connect(
            lambda rid, err: self._after_job(rid, f"✗ FAILED {rid[:8]}: {err}"))

        self.setWindowTitle("Animation Generator")
        self.resize(1180, 820)
        self._build_toolbar()
        self._build_body()
        self.reload()

    # ---- construction ---------------------------------------------------
    def _build_toolbar(self) -> None:
        tb = QToolBar("Main")
        tb.setMovable(False)
        self.addToolBar(tb)
        for label, slot in (("New config", self.new_config), ("Reload", self.reload)):
            act = QAction(label, self)
            act.triggered.connect(slot)
            tb.addAction(act)
        tb.addSeparator()
        tb.addWidget(QLabel(" Model: "))
        self.model_filter = QComboBox()
        self.model_filter.currentIndexChanged.connect(self.reload)
        tb.addWidget(self.model_filter)
        self.starred_filter = QCheckBox("Starred only")
        self.starred_filter.stateChanged.connect(self.reload)
        tb.addWidget(self.starred_filter)
        exp_view = QAction("Export view", self)
        exp_view.triggered.connect(self.export_current_view)
        tb.addAction(exp_view)
        self.cancel_act = QAction("Cancel pending", self)
        self.cancel_act.setToolTip("Cancel all queued generations that haven't started yet")
        self.cancel_act.triggered.connect(self.cancel_pending)
        self.cancel_act.setEnabled(False)
        tb.addAction(self.cancel_act)
        tb.addSeparator()
        lib_act = QAction("Model Library", self)
        lib_act.triggered.connect(self.show_library)
        tb.addAction(lib_act)
        comfy_act = QAction("Launch ComfyUI", self)
        comfy_act.setToolTip("Start the local ComfyUI backend with --disable-dynamic-vram")
        comfy_act.triggered.connect(self.launch_comfyui)
        tb.addAction(comfy_act)
        mon_act = QAction("ComfyUI Status", self)
        mon_act.setToolTip("Live status, memory, queue, settings and installed models")
        mon_act.triggered.connect(self.show_comfy_monitor)
        tb.addAction(mon_act)

    def _build_body(self) -> None:
        self.cards_container = QWidget()
        self.cards_layout = QVBoxLayout(self.cards_container)
        self.cards_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.cards_layout.setSpacing(8)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.cards_container)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setPlaceholderText("Generation log…")

        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(scroll)
        splitter.addWidget(self.log)
        splitter.setSizes([640, 160])

        central = QWidget()
        QVBoxLayout(central).addWidget(splitter)
        self.setCentralWidget(central)

    # ---- data -----------------------------------------------------------
    def reload(self) -> None:
        self._refresh_model_filter()
        model_sel = self.model_filter.currentData()
        starred_only = self.starred_filter.isChecked()
        expanded = {cid for cid, c in self.cards.items() if c.expand_btn.isChecked()}

        while self.cards_layout.count():
            w = self.cards_layout.takeAt(0).widget()
            if w:
                w.deleteLater()
        self.cards.clear()

        configs = self.store.list_configs()
        shown = 0
        for cfg in configs:
            if model_sel and cfg.model_id != model_sel:
                continue
            if starred_only and not self.store.list_results(cfg.id, starred_only=True):
                continue
            card = ConfigCard(self.store, cfg)
            card.generate_requested.connect(self.generate_config)
            card.edit_requested.connect(self.edit_config)
            card.export_results_requested.connect(self.export_results)
            if cfg.id in expanded:
                card.expand_btn.setChecked(True)
            self.cards_layout.addWidget(card)
            self.cards[cfg.id] = card
            shown += 1

        self.statusBar().showMessage(f"{shown} configs shown · {len(configs)} total")

    def _refresh_model_filter(self) -> None:
        self.model_filter.blockSignals(True)
        prev = self.model_filter.currentData()
        self.model_filter.clear()
        self.model_filter.addItem("All models", None)
        for mid in self.store.used_model_ids():
            model = library.get_model(mid)
            self.model_filter.addItem(model["display_name"] if model else mid, mid)
        idx = self.model_filter.findData(prev)
        self.model_filter.setCurrentIndex(idx if idx >= 0 else 0)
        self.model_filter.blockSignals(False)

    # ---- config actions -------------------------------------------------
    def new_config(self) -> None:
        if ConfigEditor(self.store, parent=self).exec():
            self.reload()

    def edit_config(self, config_id: str) -> None:
        if ConfigEditor(self.store, config=self.store.get_config(config_id), parent=self).exec():
            self.reload()

    def generate_config(self, config_id: str) -> None:
        cfg = self.store.get_config(config_id)
        model = library.get_model(cfg.model_id)
        if not model:
            QMessageBox.warning(self, "Generate", f"Unknown model: {cfg.model_id}")
            return
        if not cfg.start_frame:
            start, _ = QFileDialog.getOpenFileName(
                self, "Pick a start frame", str(paths.ASSETS_DIR),
                "Images (*.png *.jpg *.jpeg *.webp)")
            if not start:
                return
            self.store.update_config(cfg.id, start_frame=start)
            cfg = self.store.get_config(config_id)

        settings = {**model.get("default_params", {}), **cfg.settings}
        est = library.estimate_cost(cfg.model_id, settings)
        item = {"name": cfg.name, "model_display": model["display_name"],
                "backend": model["backend"], "est_cost": est, "params": settings}
        if not confirm_launch(self, [item]):
            self._log("launch cancelled")
            return

        snapshot = {
            "model_id": cfg.model_id, "backend": model["backend"],
            "replicate_model_id": model.get("replicate_model_id"),
            "workflow_template": model.get("workflow_template"),
            "start_frame": cfg.start_frame, "end_frame": cfg.end_frame,
            "prompt": cfg.prompt, "negative_prompt": cfg.negative_prompt, "settings": settings,
        }
        result = self.store.add_result(cfg.id, status=STATUS_PENDING,
                                       seed=settings.get("seed"), cost_estimate=est,
                                       settings_snapshot=snapshot)
        self.jobs.enqueue(result.id, model["backend"],
                          self._make_runner(model, cfg, settings, result.id))
        self._log(f"queued {result.id[:8]} ({cfg.name})")
        if cfg.id in self.cards:
            self.cards[cfg.id].refresh_results()
        self._refresh_cancel_action()

    def cancel_pending(self) -> None:
        n = self.jobs.cancel_pending()
        self._log(f"cancelled {n} pending generation(s)" if n
                  else "no pending generations to cancel")
        self._refresh_cancel_action()

    def _refresh_cancel_action(self) -> None:
        self.cancel_act.setEnabled(self.jobs.pending_count() > 0)

    def _make_runner(self, model, cfg, settings, result_id):
        out_path = paths.RESULTS_DIR / cfg.id / f"{result_id}.mp4"
        if model["backend"] == "replicate":
            rid = model["replicate_model_id"]
            data_uri = model.get("requires_data_uri", False)
            extra = {k: v for k, v in settings.items() if k not in _EXPLICIT_SETTINGS}

            def runner(progress):
                return replicate_client.generate(
                    rid, start=cfg.start_frame, end=cfg.end_frame, prompt=cfg.prompt,
                    negative=cfg.negative_prompt, duration=settings.get("duration"),
                    resolution=settings.get("resolution"), seed=settings.get("seed"),
                    extra=extra, data_uri=data_uri, out_path=out_path, progress_cb=progress)
            return runner

        tpl = paths.resolve_template(model.get("workflow_template") or "")
        roles = model.get("comfy_nodes")

        def runner(progress):
            return comfy_client.generate(
                tpl, out_path, start=cfg.start_frame, end=cfg.end_frame,
                prompt=cfg.prompt or None, negative=cfg.negative_prompt or None,
                seed=settings.get("seed"), node_roles=roles, progress_cb=progress)
        return runner

    # ---- export ---------------------------------------------------------
    def export_results(self, result_ids: list, label: Optional[str] = None) -> None:
        if not result_ids:
            QMessageBox.information(self, "Export", "Nothing to export.")
            return
        if label is None:
            cfg_ids = {r.config_id for r in (self.store.get_result(i) for i in result_ids) if r}
            if len(cfg_ids) == 1:
                cfg = self.store.get_config(next(iter(cfg_ids)))
                label = cfg.name if cfg else "selection"
            else:
                label = "selection"
        try:
            res = export.export_results(self.store, result_ids, label=label)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "Export", f"Export failed:\n{e}")
            return
        self._report_export(res)

    def export_current_view(self) -> None:
        ids = []
        for card in self.cards.values():
            ids.extend(card._row_export_ids())
        self.export_results(ids, label="view")

    def _report_export(self, res: dict) -> None:
        parent = res.get("parent")
        if not parent:
            QMessageBox.information(self, "Export", "No results had a video file to export.")
            return
        n_folders = len(res["exported"])
        total_frames = sum(n for _, n in res["exported"])
        skipped = len(res.get("skipped", []))
        msg = f"Exported {n_folders} animation(s), {total_frames} frames total\n\n{parent}"
        if skipped:
            msg += f"\n\n({skipped} skipped - no video file)"
        self._log(f"exported {n_folders} animation(s) -> {parent}")
        box = QMessageBox(self)
        box.setWindowTitle("Export complete")
        box.setText(msg)
        open_btn = box.addButton("Open folder", QMessageBox.ButtonRole.AcceptRole)
        box.addButton(QMessageBox.StandardButton.Ok)
        box.exec()
        if box.clickedButton() is open_btn:
            import os
            try:
                os.startfile(str(parent))  # type: ignore[attr-defined]  # Windows
            except Exception:  # noqa: BLE001
                pass

    def show_library(self) -> None:
        # keep a reference so the separate window isn't garbage-collected
        self._library_window = ModelLibraryWindow(self)
        self._library_window.setWindowFlag(Qt.WindowType.Window, True)
        self._library_window.show()

    def show_comfy_monitor(self) -> None:
        # keep a reference so the separate window isn't garbage-collected
        self._comfy_monitor = ComfyMonitorWindow(self)
        self._comfy_monitor.setWindowFlag(Qt.WindowType.Window, True)
        self._comfy_monitor.show()

    # ---- local backend --------------------------------------------------
    def launch_comfyui(self) -> None:
        """Start the local ComfyUI (with --disable-dynamic-vram) if it isn't already up.

        Non-blocking: a _ComfyController does the probe/launch/poll off-thread and reports
        back through _on_comfy_result, so the button never freezes the GUI.
        """
        self._log("checking ComfyUI…")
        self.statusBar().showMessage("Checking ComfyUI…")
        self._comfy_ctl = _ComfyController()  # kept on self so it isn't GC'd
        self._comfy_ctl.result.connect(self._on_comfy_result)
        self._comfy_ctl.start()

    def _on_comfy_result(self, outcome: str, info: dict) -> None:
        if outcome == "launching":
            self._log("launching ComfyUI (--disable-dynamic-vram) - first model load can take a minute…")
            self.statusBar().showMessage("Starting ComfyUI…")
            return
        if outcome == "failed":
            self.statusBar().clearMessage()
            self._log(f"ComfyUI launch failed: {info['message']}")
            QMessageBox.warning(self, "Launch ComfyUI", info["message"])
            return
        if outcome == "timeout":
            self._log("ComfyUI did not answer in time - check data/comfyui_server.log")
            self.statusBar().showMessage("ComfyUI did not start", 6000)
            return

        # outcome is 'running' (already up) or 'ready' (we just launched it)
        already = outcome == "running"
        version = info.get("version") or "?"
        if info.get("dynamic_vram"):  # via our launcher this won't happen; report it if it does
            self._log("ComfyUI up with dynamic VRAM ENABLED - stop it and relaunch")
            self.statusBar().showMessage("ComfyUI up (dynamic VRAM ENABLED!)", 6000)
            QMessageBox.warning(
                self, "ComfyUI",
                f"ComfyUI is {'already ' if already else ''}running with DYNAMIC VRAM "
                "ENABLED, so local generations will be blocked. Stop that server, then "
                "click Launch ComfyUI again to start one with --disable-dynamic-vram.")
        else:
            self._log(f"ComfyUI {'already ' if already else ''}ready (v{version}) - dynamic VRAM disabled")
            self.statusBar().showMessage("ComfyUI ready", 6000)
            if already:
                QMessageBox.information(
                    self, "ComfyUI",
                    f"ComfyUI is already running (v{version}) with dynamic VRAM disabled. "
                    "Ready to generate.")

    # ---- job signal handlers -------------------------------------------
    def _log(self, line: str) -> None:
        self.log.appendPlainText(line)

    def _card_for_result(self, result_id: str):
        r = self.store.get_result(result_id)
        return self.cards.get(r.config_id) if r else None

    def _on_progress(self, result_id: str, line: str) -> None:
        self._log(f"  {result_id[:8]}: {line}")

    def _on_status_changed(self, result_id: str, status: str) -> None:
        self._log(f"[{status}] {result_id[:8]}")
        card = self._card_for_result(result_id)
        if card:
            card.refresh_results()
        self._refresh_cancel_action()

    def _after_job(self, result_id: str, msg: str) -> None:
        self._log(msg)
        card = self._card_for_result(result_id)
        if card:
            card.refresh_results()
        self._refresh_cancel_action()
