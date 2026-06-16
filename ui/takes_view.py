"""Takes folder view - the per-shot grid of generated takes.

A QListView in IconMode (Windows-folder style) with an icon-size slider, a
favorite/all filter, live status badges, per-take star + delete-to-bin, and
shift/ctrl multi-select. Thumbnails are the first video frame, generated lazily and
cached. Emits `changed` (so the card header can refresh counts) and `export_requested`.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QComboBox, QHBoxLayout, QLabel, QListView, QMenu, QPushButton, QSlider,
    QVBoxLayout, QWidget,
)

from pipeline import extract, takes_io
from store.project import Project

_USER_ROLE = int(Qt.ItemDataRole.UserRole)
_BADGE = {"pending": "⏳", "generating": "▶", "done": "", "failed": "✗"}
_BADGE_COLOR = {"pending": "#b0b0b0", "generating": "#5aa0ff",
                "done": "#7ade8c", "failed": "#ff6b6b", "cancelled": "#c0a060"}


class TakesView(QWidget):
    changed = Signal()
    export_requested = Signal(list)   # list[take_id]

    def __init__(self, project: Project, shot_id: str):
        super().__init__()
        self.project = project
        self.shot_id = shot_id
        self._build()
        self.load()

    def _build(self) -> None:
        self.filter = QComboBox()
        self.filter.addItems(["All", "Favorites"])
        self.filter.currentIndexChanged.connect(self.load)

        self.size_slider = QSlider(Qt.Orientation.Horizontal)
        self.size_slider.setRange(80, 320)
        self.size_slider.setValue(140)
        self.size_slider.setFixedWidth(120)
        self.size_slider.valueChanged.connect(self._apply_icon_size)

        export_btn = QPushButton("Export selected")
        export_btn.clicked.connect(
            lambda: self.export_requested.emit(self.selected_take_ids()))
        self.count_label = QLabel("")

        head = QHBoxLayout()
        head.addWidget(QLabel("View:")); head.addWidget(self.filter)
        head.addStretch(1)
        head.addWidget(self.count_label)
        head.addWidget(QLabel("Size:")); head.addWidget(self.size_slider)
        head.addWidget(export_btn)

        self.model = QStandardItemModel()
        self.view = QListView()
        self.view.setModel(self.model)
        self.view.setViewMode(QListView.ViewMode.IconMode)
        self.view.setResizeMode(QListView.ResizeMode.Adjust)
        self.view.setMovement(QListView.Movement.Static)
        self.view.setSelectionMode(QListView.SelectionMode.ExtendedSelection)
        self.view.setWordWrap(True)
        self.view.setSpacing(8)
        self.view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self._context_menu)
        self.view.doubleClicked.connect(self._open_selected)
        self._apply_icon_size()

        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 0, 6, 6)
        lay.addLayout(head)
        lay.addWidget(self.view)

    # ---- population -----------------------------------------------------
    def load(self) -> None:
        fav = self.filter.currentText() == "Favorites"
        takes = self.project.list_takes(self.shot_id, starred_only=fav)
        self.model.clear()
        for t in takes:
            item = QStandardItem(self._icon_for(t), self._label(t))
            item.setData(t.id, _USER_ROLE)
            item.setEditable(False)
            self.model.appendRow(item)
        self.count_label.setText(f"{len(takes)} shown")

    def _label(self, t) -> str:
        badge = _BADGE.get(t.status, "")
        star = "★ " if t.starred else ""
        tail = "" if t.status == "done" else f"  {t.status}"
        return f"{star}{badge}{tail}".strip() or t.id[:6]

    def _icon_for(self, t) -> QIcon:
        if t.thumbnail and Path(t.thumbnail).exists():
            return QIcon(t.thumbnail)
        if t.video_path and Path(t.video_path).exists():
            try:
                out = self.project.thumbs_dir / f"{t.id}.png"
                if not out.exists():
                    extract.make_thumbnail(t.video_path, out)
                if out.exists():
                    self.project.update_take(t.id, thumbnail=str(out))
                    return QIcon(str(out))
            except Exception:  # noqa: BLE001 - corrupt/locked video -> placeholder
                pass
        return self._placeholder(t.status)

    def _placeholder(self, status: str) -> QIcon:
        pm = QPixmap(220, 160)
        pm.fill(QColor("#222"))
        p = QPainter(pm)
        p.setPen(QColor(_BADGE_COLOR.get(status, "#999")))
        p.drawText(pm.rect(), Qt.AlignmentFlag.AlignCenter, status)
        p.end()
        return QIcon(pm)

    def _apply_icon_size(self) -> None:
        s = self.size_slider.value()
        self.view.setIconSize(QSize(s, s))
        self.view.setGridSize(QSize(s + 26, s + 42))

    # ---- selection / actions -------------------------------------------
    def selected_take_ids(self) -> list:
        return [self.model.itemFromIndex(i).data(_USER_ROLE)
                for i in self.view.selectedIndexes()]

    def all_take_ids(self) -> list:
        return [self.model.item(r).data(_USER_ROLE) for r in range(self.model.rowCount())]

    def _context_menu(self, pos) -> None:
        ids = self.selected_take_ids()
        if not ids:
            return
        menu = QMenu(self)
        act_star = menu.addAction("Toggle star")
        act_del = menu.addAction("Delete (to bin)")
        act_exp = menu.addAction("Export selected")
        act_open = menu.addAction("Open video")
        chosen = menu.exec(self.view.mapToGlobal(pos))
        if chosen == act_star:
            self.toggle_star(ids)
        elif chosen == act_del:
            self.delete(ids)
        elif chosen == act_exp:
            self.export_requested.emit(ids)
        elif chosen == act_open:
            self._open_selected()

    def toggle_star(self, ids: list) -> None:
        for tid in ids:
            t = self.project.get_take(tid)
            if t:
                self.project.set_starred(tid, not t.starred)
        self.load()
        self.changed.emit()

    def delete(self, ids: list) -> None:
        for tid in ids:
            t = self.project.get_take(tid)
            if t:
                takes_io.move_to_bin(t, self.project)
        self.load()
        self.changed.emit()

    def _open_selected(self, *_) -> None:
        import os
        for tid in self.selected_take_ids():
            t = self.project.get_take(tid)
            if t and t.video_path and Path(t.video_path).exists():
                try:
                    os.startfile(t.video_path)  # type: ignore[attr-defined]  # Windows
                except Exception:  # noqa: BLE001
                    pass
