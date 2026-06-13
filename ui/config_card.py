"""ConfigCard - one animation-configuration row.

Header shows name, start/end frame thumbnails, prompt snippet, model, settings
summary, and Generate/Edit/Export buttons + an expand toggle that reveals the
inline ResultsView (the folder of takes for this config). Re-emits the results
view's `changed` so the leader can keep counts fresh.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QPushButton, QToolButton, QVBoxLayout, QWidget,
)

import library
from store.db import Store
from ui.results_view import ResultsView


def _thumb(path: Optional[str], size: int = 64) -> QPixmap:
    pm = QPixmap(size, size)
    if path and Path(path).exists():
        loaded = QPixmap(path)
        if not loaded.isNull():
            return loaded.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio,
                                 Qt.TransformationMode.SmoothTransformation)
    pm.fill(Qt.GlobalColor.darkGray)
    return pm


class ConfigCard(QFrame):
    generate_requested = Signal(str)
    edit_requested = Signal(str)
    export_results_requested = Signal(list)   # result ids (row obeys its view filter)
    changed = Signal()

    def __init__(self, store: Store, config):
        super().__init__()
        self.store = store
        self.config = config
        self.results_view: Optional[ResultsView] = None
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setStyleSheet("ConfigCard { border:1px solid #3a3f4b; border-radius:6px; }")
        self._build()

    def _build(self) -> None:
        cfg = self.config
        self.expand_btn = QToolButton()
        self.expand_btn.setArrowType(Qt.ArrowType.RightArrow)
        self.expand_btn.setCheckable(True)
        self.expand_btn.toggled.connect(self._on_toggle)

        self.start_thumb = QLabel(); self.start_thumb.setPixmap(_thumb(cfg.start_frame))
        self.end_thumb = QLabel(); self.end_thumb.setPixmap(_thumb(cfg.end_frame))

        name = QLabel(f"<b>{cfg.name}</b>")
        model = library.get_model(cfg.model_id)
        model_name = model["display_name"] if model else (cfg.model_id or "(no model)")
        prompt = (cfg.prompt or "").strip().replace("\n", " ")
        if len(prompt) > 70:
            prompt = prompt[:70] + "…"
        seed = cfg.settings.get("seed")
        dur = cfg.settings.get("duration")
        res = cfg.settings.get("resolution")
        summary = "  ".join(s for s in (
            f"seed {seed}" if seed is not None else "",
            f"{dur}s" if dur else "",
            str(res) if res else "") if s)

        info = QVBoxLayout()
        info.setSpacing(2)
        info.addWidget(name)
        info.addWidget(QLabel(f"<span style='color:#9aa'>{model_name}</span>   {summary}"))
        info.addWidget(QLabel(f"<span style='color:#bbb'>{prompt or '(no prompt)'}</span>"))

        self.counts = QLabel("")
        gen_btn = QPushButton("Generate"); gen_btn.clicked.connect(
            lambda: self.generate_requested.emit(self.config.id))
        edit_btn = QPushButton("Edit"); edit_btn.clicked.connect(
            lambda: self.edit_requested.emit(self.config.id))
        exp_btn = QPushButton("Export row"); exp_btn.clicked.connect(
            lambda: self.export_results_requested.emit(self._row_export_ids()))

        header = QHBoxLayout()
        header.addWidget(self.expand_btn)
        header.addWidget(self.start_thumb)
        header.addWidget(self.end_thumb)
        header.addLayout(info, 1)
        header.addWidget(self.counts)
        header.addWidget(gen_btn)
        header.addWidget(edit_btn)
        header.addWidget(exp_btn)

        self.body = QWidget()
        self.body.setVisible(False)
        QVBoxLayout(self.body).setContentsMargins(0, 0, 0, 0)

        lay = QVBoxLayout(self)
        hw = QWidget(); hw.setLayout(header)
        lay.addWidget(hw)
        lay.addWidget(self.body)
        self.refresh_counts()

    # ---- expand / results ----------------------------------------------
    def _on_toggle(self, on: bool) -> None:
        self.expand_btn.setArrowType(Qt.ArrowType.DownArrow if on else Qt.ArrowType.RightArrow)
        if on and self.results_view is None:
            self.results_view = ResultsView(self.store, self.config.id)
            self.results_view.changed.connect(self._on_results_changed)
            self.results_view.export_requested.connect(self.export_results_requested)
            self.body.layout().addWidget(self.results_view)
        self.body.setVisible(on)

    def _on_results_changed(self) -> None:
        self.refresh_counts()
        self.changed.emit()

    def refresh_results(self) -> None:
        if self.results_view is not None:
            self.results_view.load()
        self.refresh_counts()

    def _row_export_ids(self) -> list:
        """Result ids to export for this row: what the results view currently shows
        (obeying its favorite/all filter), or all results if not yet expanded."""
        if self.results_view is not None:
            return self.results_view.all_result_ids()
        return [r.id for r in self.store.list_results(self.config.id)]

    def refresh_counts(self) -> None:
        results = self.store.list_results(self.config.id)
        n = len(results)
        n_star = len([r for r in results if r.starred])
        active = [r for r in results if r.status in ("pending", "generating")]
        badge = f"  {len(active)}⏳" if active else ""
        self.counts.setText(f"{n} results" + (f" · {n_star}★" if n_star else "") + badge)
