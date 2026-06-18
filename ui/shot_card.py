"""ShotCard - one shot (animation) row.

Header shows name, start/end keypose thumbnails, prompt snippet, model, settings
summary, and Generate/Edit/Export buttons + an expand toggle that reveals the inline
TakesView (the folder of takes for this shot). Re-emits the takes view's `changed` so
the leader can keep counts fresh.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QMenu, QPushButton, QToolButton, QVBoxLayout, QWidget,
)

import library
from pipeline import framing
from store.project import Project
from ui.placement_widget import pil_to_pixmap
from ui.takes_view import TakesView

# Keyed sprites are placement-independent, so cache them (keyed by path + stat so a
# reused filename with new content misses): the Shots list keys each asset at most once
# and reloads / shots sharing an asset are instant.
_KEYED_CACHE: dict = {}
_THUMB_KEY_MAX = 384   # cap the keyed-sprite source resolution - thumbnails don't need the
                       # full 1254 contract canvas, and keying full-res for every row stalls
                       # the first Shots-list paint (a 30+ shot project = 60+ keyings).


def _thumb_canvas(shot, long: int = 88) -> tuple[int, int]:
    """A small thumbnail canvas matching the shot's generation aspect."""
    w = getattr(shot, "canvas_w", None) or 1254
    h = getattr(shot, "canvas_h", None) or 1254
    if w >= h:
        return long, max(1, round(long * h / w))
    return max(1, round(long * w / h)), long


def _placeholder(size: tuple[int, int]) -> QPixmap:
    pm = QPixmap(size[0], size[1])
    pm.fill(Qt.GlobalColor.darkGray)
    return pm


def framed_thumb(shot, which: str, long: int = 88) -> QPixmap:
    """The shot's start/end keyframe AS FRAMED - the keyed sprite placed on the magenta
    aspect canvas per shot.crop, i.e. what actually gets generated (mirrors
    shot_tab._framed_pixmap). Missing/unreadable asset -> a gray placeholder."""
    canvas = _thumb_canvas(shot, long)
    asset = getattr(shot, "start_frame" if which == "start" else "end_frame", None)
    if not (asset and Path(asset).exists()):
        return _placeholder(canvas)
    try:
        st = os.stat(asset)
        cache_key = (asset, st.st_mtime_ns, st.st_size)
        sprite = _KEYED_CACHE.get(cache_key)
        if sprite is None:
            sprite = framing.keyed_sprite(asset, max_side=_THUMB_KEY_MAX, crop_to_content=False)
            _KEYED_CACHE[cache_key] = sprite
        placement = (shot.crop or {}).get(which) or {}
        return pil_to_pixmap(framing.render_placement(asset, placement, canvas, sprite=sprite))
    except Exception:  # noqa: BLE001 - unreadable image -> placeholder
        return _placeholder(canvas)


class ShotCard(QFrame):
    generate_requested = Signal(str)
    open_requested = Signal(str)            # open the shot in its own tab (= Edit)
    duplicate_requested = Signal(str)       # copy the shot into a new one
    delete_requested = Signal(str)          # remove the shot (+ its takes)
    star_toggled = Signal(str)              # toggle the shot's own star
    export_takes_requested = Signal(list)   # take ids (row obeys its view filter)
    open_take_requested = Signal(str)       # take id -> open in the frame-by-frame viewer
    changed = Signal()

    def __init__(self, project: Project, shot, jobs=None):
        super().__init__()
        self.project = project
        self.shot = shot
        self.jobs = jobs
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

    # Right-click the row for the common per-shot actions. Built in a helper (not inline)
    # so the menu + its action wiring can be exercised headlessly without exec().
    def contextMenuEvent(self, event) -> None:  # noqa: N802 - Qt override
        self._build_context_menu().exec(event.globalPos())

    def _build_context_menu(self) -> QMenu:
        sid = self.shot.id
        menu = QMenu(self)
        menu.addAction("Edit").triggered.connect(lambda: self.open_requested.emit(sid))
        menu.addAction("Generate").triggered.connect(lambda: self.generate_requested.emit(sid))
        menu.addAction("Duplicate").triggered.connect(lambda: self.duplicate_requested.emit(sid))
        star_label = "Unstar shot" if self.shot.starred else "Star shot"
        menu.addAction(star_label).triggered.connect(lambda: self.star_toggled.emit(sid))
        menu.addSeparator()
        menu.addAction("Delete").triggered.connect(lambda: self.delete_requested.emit(sid))
        return menu

    def _build(self) -> None:
        shot = self.shot
        self.expand_btn = QToolButton()
        self.expand_btn.setArrowType(Qt.ArrowType.RightArrow)
        self.expand_btn.setCheckable(True)
        self.expand_btn.toggled.connect(self._on_toggle)

        self.start_thumb = QLabel(); self.start_thumb.setPixmap(framed_thumb(shot, "start"))
        self.end_thumb = QLabel(); self.end_thumb.setPixmap(framed_thumb(shot, "end"))

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
            f"seed {library.seed_label(seed)}" if seed is not None else "",
            f"{dur}s" if dur else "",
            str(res) if res else "") if s)

        info = QVBoxLayout()
        info.setSpacing(2)
        info.addWidget(name)
        info.addWidget(QLabel(f"<span style='color:#9aa'>{model_name}</span>   {summary}"))
        info.addWidget(QLabel(f"<span style='color:#bbb'>{prompt or '(no prompt)'}</span>"))

        self.counts = QLabel("")
        self.star_btn = QToolButton()
        self.star_btn.setCheckable(True)
        self.star_btn.setAutoRaise(True)
        self.star_btn.setToolTip("Star this shot")
        self._refresh_star_btn()
        self.star_btn.clicked.connect(lambda: self.star_toggled.emit(self.shot.id))
        gen_btn = QPushButton("Generate"); gen_btn.clicked.connect(
            lambda: self.generate_requested.emit(self.shot.id))
        exp_btn = QPushButton("Export row"); exp_btn.clicked.connect(
            lambda: self.export_takes_requested.emit(self._row_export_ids()))

        header = QHBoxLayout()
        header.addWidget(self.expand_btn)
        header.addWidget(self.star_btn)
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

    def _refresh_star_btn(self) -> None:
        on = bool(self.shot.starred)
        self.star_btn.setChecked(on)
        self.star_btn.setText("★" if on else "☆")

    # ---- expand / takes -------------------------------------------------
    def _on_toggle(self, on: bool) -> None:
        self.expand_btn.setArrowType(Qt.ArrowType.DownArrow if on else Qt.ArrowType.RightArrow)
        if on and self.takes_view is None:
            self.takes_view = TakesView(self.project, self.shot.id, jobs=self.jobs)
            self.takes_view.changed.connect(self._on_takes_changed)
            self.takes_view.export_requested.connect(self.export_takes_requested)
            self.takes_view.open_take_requested.connect(self.open_take_requested)
            self.body.layout().addWidget(self.takes_view)
        self.body.setVisible(on)
        if self.takes_view is not None:
            self.takes_view.set_animating(on)   # don't decode gifs for a collapsed row

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
