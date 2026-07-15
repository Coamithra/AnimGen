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
    QDialog, QFileDialog, QHBoxLayout, QLabel, QListView, QMenu, QMessageBox, QPushButton,
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
                dest = self.project.import_asset(f)
            except Exception:  # noqa: BLE001 - skip unreadable drops
                continue
            try:
                self._prepare_imported_asset(dest)   # force a bg composite if it's transparent
            except Exception:  # noqa: BLE001 - a prep failure must not lose the import
                pass
            added += 1
        if added:
            self.load()
            self.changed.emit()

    def _prepare_imported_asset(self, dest) -> None:
        """Video models can't take transparency, so an imported RGBA-with-alpha asset gets its
        transparent areas composited onto the contract magenta at import time (no keying — it's
        already transparent). The original transparent sprite is stored as this asset's
        reference so a later Replace background can re-fill it losslessly."""
        from pipeline import bg_replace
        img = bg_replace.load_image(dest)
        if not bg_replace.has_transparency(img):
            return
        transparent = img.convert("RGBA")
        opaque = bg_replace.composite_over(transparent, bg_replace.CONTRACT_FILL)
        opaque.save(dest)
        self.project.store_transparent_ref(
            dest, transparent, imported_transparent=True,
            target_fill=list(bg_replace.CONTRACT_FILL))

    def _context_menu(self, pos) -> None:
        idxs = self.view.selectedIndexes()
        if not idxs:
            return
        targets = [self.model.itemFromIndex(i).data(_USER_ROLE) for i in idxs]
        menu, acts = self._build_context_menu(targets)
        chosen = menu.exec(self.view.mapToGlobal(pos))
        if chosen is None:
            return
        if chosen is acts["delete"]:
            self._delete(targets)
        elif chosen is acts["replace_bg"]:
            self._replace_background(targets)

    def _build_context_menu(self, targets):
        """Build the right-click menu (no .exec(), so it's headless-testable). Returns
        (menu, {action_key: QAction})."""
        menu = QMenu(self)
        acts = {"replace_bg": menu.addAction("Replace background…")}
        menu.addSeparator()
        acts["delete"] = menu.addAction(f"Delete {len(targets)} asset(s)")
        return menu, acts

    # ---- replace background ---------------------------------------------
    def _replace_background(self, targets) -> None:
        from pipeline import bg_replace
        from ui.bg_replace_dialog import BackgroundReplaceDialog
        try:
            corner = bg_replace.sample_corner(bg_replace.load_image(targets[0]))
            prefill = bg_replace.nearest_chroma(corner) or bg_replace.AUTO
        except Exception:  # noqa: BLE001
            prefill = bg_replace.AUTO
        # A stored transparent reference is reused (a re-fill), so the source screen won't be
        # re-keyed - reflect that in the dialog instead of offering an inert source choice.
        reusing = bool(self.project.asset_meta(targets[0]).get("transparent_ref"))
        dlg = BackgroundReplaceDialog(prefill, reusing=reusing, parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        self._apply_replace_background(targets, dlg.source(), dlg.fill_rgb())

    def _apply_replace_background(self, targets, source, fill_rgb) -> None:
        """Key `source` out of each target and composite onto `fill_rgb`, overwriting the asset
        in place (same path, so shots keep referencing it). Reuses a stored transparent
        reference when present (a lossless re-fill that does NOT re-key, so it records only the
        new fill - never a `source_chroma` that wasn't actually applied), else keys the current
        asset and stores the result as the reference."""
        from pipeline import bg_replace
        done = 0
        for pth in targets:
            try:
                transparent = self.project.transparent_ref(pth)
                if transparent is not None:
                    opaque = bg_replace.composite_over(transparent, fill_rgb)
                    opaque.save(pth)
                    self.project.set_asset_meta(pth, target_fill=list(fill_rgb))
                else:
                    img = bg_replace.load_image(pth)
                    opaque, transparent = bg_replace.replace_background(img, source, fill_rgb)
                    opaque.save(pth)
                    self.project.store_transparent_ref(
                        pth, transparent, source_chroma=source, target_fill=list(fill_rgb))
                done += 1
            except Exception:  # noqa: BLE001 - one asset's failure shouldn't abort the rest
                pass
        if done < len(targets):
            QMessageBox.warning(
                self, "Replace background",
                f"Replaced the background on {done} of {len(targets)} asset(s); "
                "the rest could not be processed.")
        self.load()
        self.changed.emit()

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
