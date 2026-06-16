"""Visual keyframe picker - a thumbnail-grid dialog for choosing a project asset.

Used by the shot tab's start/end keyframe buttons. Shows the project's assets as a grid
of thumbnails (double-click or OK to choose) and can Import new images on the spot.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QIcon, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout, QLabel, QListView,
    QPushButton, QVBoxLayout,
)

from store.project import Project

_USER_ROLE = int(Qt.ItemDataRole.UserRole)
_FILTER = "Images (*.png *.jpg *.jpeg *.webp *.bmp)"


class AssetPickerDialog(QDialog):
    def __init__(self, project: Project, current: Optional[str] = None, parent=None):
        super().__init__(parent)
        self.project = project
        self.setWindowTitle("Choose keyframe")
        self.resize(700, 540)
        self._build(current)

    def _build(self, current: Optional[str]) -> None:
        self.model = QStandardItemModel()
        self.view = QListView()
        self.view.setModel(self.model)
        self.view.setViewMode(QListView.ViewMode.IconMode)
        self.view.setResizeMode(QListView.ResizeMode.Adjust)
        self.view.setMovement(QListView.Movement.Static)
        self.view.setIconSize(QSize(120, 120))
        self.view.setGridSize(QSize(150, 168))
        self.view.setWordWrap(True)
        self.view.setSpacing(8)
        self.view.doubleClicked.connect(lambda _i: self.accept())

        import_btn = QPushButton("Import…")
        import_btn.clicked.connect(self._import)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                              | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        foot = QHBoxLayout()
        foot.addWidget(import_btn)
        foot.addStretch(1)
        foot.addWidget(bb)

        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("Pick a keyframe (double-click to choose) — or Import to add one:"))
        lay.addWidget(self.view, 1)
        lay.addLayout(foot)
        self._reload(select=current)

    def _reload(self, select: Optional[str] = None) -> None:
        self.model.clear()
        for p in self.project.list_assets():
            item = QStandardItem(QIcon(str(p)), p.name)
            item.setData(str(p), _USER_ROLE)
            item.setEditable(False)
            item.setToolTip(str(p))
            self.model.appendRow(item)
            if select and str(p) == select:
                self.view.setCurrentIndex(item.index())

    def _import(self) -> None:
        import paths
        files, _ = QFileDialog.getOpenFileNames(
            self, "Import images", str(paths.ASSETS_DIR), _FILTER)
        last = None
        for f in files:
            try:
                last = self.project.import_asset(f)
            except Exception:  # noqa: BLE001 - skip unreadable picks
                pass
        if last:
            self._reload(select=str(last))

    def selected(self) -> Optional[str]:
        idxs = self.view.selectedIndexes()
        return self.model.itemFromIndex(idxs[0]).data(_USER_ROLE) if idxs else None
