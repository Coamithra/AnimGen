"""Assets tab - the project's keyframe image library.

A drag-and-drop grid of the project's assets: image files kept flat in the project's
.assets/ folder. Drop images here (or use Import) to copy them into the project; shots
reference these assets for their start/end keyframes. Right-click to delete.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QIcon, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QFileDialog, QHBoxLayout, QLabel, QListView, QMenu, QMessageBox, QPushButton,
    QVBoxLayout, QWidget,
)

from store.project import Project

_USER_ROLE = int(Qt.ItemDataRole.UserRole)
_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".bmp")
_FILTER = "Images (*.png *.jpg *.jpeg *.webp *.bmp)"


class AssetsView(QWidget):
    changed = Signal()   # assets added/removed

    def __init__(self, project: Project, parent=None):
        super().__init__(parent)
        self.project = project
        self.setAcceptDrops(True)
        self._build()
        self.load()

    def set_project(self, project: Project) -> None:
        self.project = project
        self.load()

    def _build(self) -> None:
        import_btn = QPushButton("Import images…")
        import_btn.clicked.connect(self._import)
        self.count_label = QLabel("")
        head = QHBoxLayout()
        head.addWidget(QLabel("Project keyframes — drag images in to add"))
        head.addStretch(1)
        head.addWidget(self.count_label)
        head.addWidget(import_btn)

        self.model = QStandardItemModel()
        self.view = QListView()
        self.view.setModel(self.model)
        self.view.setViewMode(QListView.ViewMode.IconMode)
        self.view.setResizeMode(QListView.ResizeMode.Adjust)
        self.view.setMovement(QListView.Movement.Static)
        self.view.setIconSize(QSize(140, 140))
        self.view.setGridSize(QSize(168, 184))
        self.view.setWordWrap(True)
        self.view.setSpacing(8)
        self.view.setSelectionMode(QListView.SelectionMode.ExtendedSelection)
        self.view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self._context_menu)

        lay = QVBoxLayout(self)
        lay.addLayout(head)
        lay.addWidget(self.view)

    def load(self) -> None:
        self.model.clear()
        assets = self.project.list_assets()
        for p in assets:
            item = QStandardItem(QIcon(str(p)), p.name)
            item.setData(str(p), _USER_ROLE)
            item.setEditable(False)
            item.setToolTip(str(p))
            self.model.appendRow(item)
        self.count_label.setText(f"{len(assets)} assets")

    # ---- import / delete ------------------------------------------------
    def _import(self) -> None:
        import paths
        files, _ = QFileDialog.getOpenFileNames(
            self, "Import images", str(paths.ASSETS_DIR), _FILTER)
        self._import_files(files)

    def _import_files(self, files) -> None:
        added = 0
        for f in files:
            try:
                self.project.import_asset(f)
                added += 1
            except Exception:  # noqa: BLE001 - skip unreadable drops
                pass
        if added:
            self.load()
            self.changed.emit()

    def _context_menu(self, pos) -> None:
        idxs = self.view.selectedIndexes()
        if not idxs:
            return
        menu = QMenu(self)
        act_del = menu.addAction(f"Delete {len(idxs)} asset(s)")
        if menu.exec(self.view.mapToGlobal(pos)) == act_del:
            self._delete([self.model.itemFromIndex(i).data(_USER_ROLE) for i in idxs])

    def _delete(self, targets) -> None:
        if QMessageBox.question(
                self, "Delete assets",
                f"Delete {len(targets)} asset file(s) from the project?\n"
                "Shots referencing them will lose their keyframe.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        for p in targets:
            self.project.remove_asset(p)
        self.load()
        self.changed.emit()

    # ---- drag & drop ----------------------------------------------------
    def _urls_to_images(self, mime) -> list:
        return [u.toLocalFile() for u in mime.urls()
                if u.isLocalFile() and Path(u.toLocalFile()).suffix.lower() in _EXTS]

    def dragEnterEvent(self, event) -> None:  # noqa: N802 - Qt override
        if event.mimeData().hasUrls() and self._urls_to_images(event.mimeData()):
            event.acceptProposedAction()

    def dragMoveEvent(self, event) -> None:  # noqa: N802 - Qt override
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:  # noqa: N802 - Qt override
        files = self._urls_to_images(event.mimeData())
        if files:
            self._import_files(files)
            event.acceptProposedAction()
