"""Main window.

Shows animation configs as expandable cards (header + inline results folder view),
with global filters (model, starred). Generate resolves a config's model + params,
runs the cost-confirm gate, creates a pending result with an immutable
settings_snapshot, and enqueues a background job whose status streams into the log
panel and refreshes the originating card. Export is wired in Phase 5.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
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
from ui.config_editor import ConfigEditor
from ui.cost_confirm import confirm_launch
from ui.model_library_window import ModelLibraryWindow

# settings keys passed to the hosted client explicitly (everything else -> extra/--set)
_EXPLICIT_SETTINGS = ("duration", "resolution", "seed", "length")


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
        tb.addSeparator()
        lib_act = QAction("Model Library", self)
        lib_act.triggered.connect(self.show_library)
        tb.addAction(lib_act)

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

    def _after_job(self, result_id: str, msg: str) -> None:
        self._log(msg)
        card = self._card_for_result(result_id)
        if card:
            card.refresh_results()
