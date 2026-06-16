"""ShotCard - one shot (animation) row.

Header shows name, start/end keypose thumbnails, prompt snippet, model, settings
summary, and Generate/Edit/Export buttons + an expand toggle that reveals the inline
TakesView (the folder of takes for this shot). Re-emits the takes view's `changed` so
the leader can keep counts fresh.
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
from store.project import Project
from ui.takes_view import TakesView


def _thumb(path: Optional[str], size: int = 64) -> QPixmap:
    pm = QPixmap(size, size)
    if path and Path(path).exists():
        loaded = QPixmap(path)
        if not loaded.isNull():
            return loaded.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio,
                                 Qt.TransformationMode.SmoothTransformation)
    pm.fill(Qt.GlobalColor.darkGray)
    return pm


class ShotCard(QFrame):
    generate_requested = Signal(str)
    open_requested = Signal(str)            # open the shot in its own tab
    export_takes_requested = Signal(list)   # take ids (row obeys its view filter)
    changed = Signal()

    def __init__(self, project: Project, shot):
        super().__init__()
        self.project = project
        self.shot = shot
        self.takes_view: Optional[TakesView] = None
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setStyleSheet("ShotCard { border:1px solid #3a3f4b; border-radius:6px; }")
        self.setToolTip("Double-click to open this shot in its own tab")
        self._build()

    # Double-clicking the row (anywhere not handled by a child button) opens the shot
    # tab; the expand arrow / Generate / Export buttons consume their own events.
    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802 - Qt override
        self.open_requested.emit(self.shot.id)
        super().mouseDoubleClickEvent(event)

    def _build(self) -> None:
        shot = self.shot
        self.expand_btn = QToolButton()
        self.expand_btn.setArrowType(Qt.ArrowType.RightArrow)
        self.expand_btn.setCheckable(True)
        self.expand_btn.toggled.connect(self._on_toggle)

        self.start_thumb = QLabel(); self.start_thumb.setPixmap(_thumb(shot.start_frame))
        self.end_thumb = QLabel(); self.end_thumb.setPixmap(_thumb(shot.end_frame))

        name = QLabel(f"<b>{shot.name}</b>")
        model = library.get_model(shot.model_id)
        model_name = model["display_name"] if model else (shot.model_id or "(no model)")
        prompt = (shot.prompt or "").strip().replace("\n", " ")
        if len(prompt) > 70:
            prompt = prompt[:70] + "…"
        seed = shot.settings.get("seed")
        dur = shot.settings.get("duration")
        res = shot.settings.get("resolution")
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
            lambda: self.generate_requested.emit(self.shot.id))
        exp_btn = QPushButton("Export row"); exp_btn.clicked.connect(
            lambda: self.export_takes_requested.emit(self._row_export_ids()))

        header = QHBoxLayout()
        header.addWidget(self.expand_btn)
        header.addWidget(self.start_thumb)
        header.addWidget(self.end_thumb)
        header.addLayout(info, 1)
        header.addWidget(self.counts)
        header.addWidget(gen_btn)
        header.addWidget(exp_btn)

        self.body = QWidget()
        self.body.setVisible(False)
        QVBoxLayout(self.body).setContentsMargins(0, 0, 0, 0)

        lay = QVBoxLayout(self)
        hw = QWidget(); hw.setLayout(header)
        lay.addWidget(hw)
        lay.addWidget(self.body)
        self.refresh_counts()

    # ---- expand / takes -------------------------------------------------
    def _on_toggle(self, on: bool) -> None:
        self.expand_btn.setArrowType(Qt.ArrowType.DownArrow if on else Qt.ArrowType.RightArrow)
        if on and self.takes_view is None:
            self.takes_view = TakesView(self.project, self.shot.id)
            self.takes_view.changed.connect(self._on_takes_changed)
            self.takes_view.export_requested.connect(self.export_takes_requested)
            self.body.layout().addWidget(self.takes_view)
        self.body.setVisible(on)

    def _on_takes_changed(self) -> None:
        self.refresh_counts()
        self.changed.emit()

    def refresh_takes(self) -> None:
        if self.takes_view is not None:
            self.takes_view.load()
        self.refresh_counts()

    def _row_export_ids(self) -> list:
        """Take ids to export for this row: what the takes view currently shows
        (obeying its favorite/all filter), or all takes if not yet expanded."""
        if self.takes_view is not None:
            return self.takes_view.all_take_ids()
        return [t.id for t in self.project.list_takes(self.shot.id)]

    def refresh_counts(self) -> None:
        takes = self.project.list_takes(self.shot.id)
        n = len(takes)
        n_star = len([t for t in takes if t.starred])
        active = [t for t in takes if t.status in ("pending", "generating")]
        badge = f"  {len(active)}⏳" if active else ""
        self.counts.setText(f"{n} takes" + (f" · {n_star}★" if n_star else "") + badge)
