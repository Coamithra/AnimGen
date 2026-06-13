"""Crop / framing widget.

A QGraphicsView shows the source image with a crop rectangle you can drag (move)
or resize by its 8 handles; numeric x/y/w/h fields stay two-way synced. Canvas size,
character-height %, ground line and horizontal placement drive pipeline.framing,
which produces a contract-normalized keypose previewed live on the right.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QImage, QPen, QPixmap
from PySide6.QtWidgets import (
    QDoubleSpinBox, QFormLayout, QGraphicsRectItem, QGraphicsScene,
    QGraphicsView, QGroupBox, QHBoxLayout, QLabel, QPushButton, QSpinBox,
    QVBoxLayout, QWidget,
)

from pipeline import framing

_EDGES = {"tl": ("l", "t"), "t": ("t",), "tr": ("r", "t"), "r": ("r",),
          "br": ("r", "b"), "b": ("b",), "bl": ("l", "b"), "l": ("l",)}


def pil_to_qpixmap(im) -> QPixmap:
    im = im.convert("RGB")
    qimg = QImage(im.tobytes("raw", "RGB"), im.width, im.height, im.width * 3,
                  QImage.Format.Format_RGB888)
    return QPixmap.fromImage(qimg.copy())


class CropRectItem(QGraphicsRectItem):
    """Crop rect kept in scene coords (item pos fixed at 0,0). Move = drag interior,
    resize = drag a handle. Reports changes via on_change."""

    def __init__(self, bounds: QRectF, on_change: Callable[[QRectF], None]):
        super().__init__(QRectF(bounds))
        self.bounds = QRectF(bounds)
        self.on_change = on_change
        self.handle_px = max(6.0, min(bounds.width(), bounds.height()) * 0.02)
        self._handle: Optional[str] = None
        self._press = QPointF()
        self._press_rect = QRectF()
        self.setPen(QPen(QColor(80, 200, 120), 0, Qt.PenStyle.SolidLine))
        self.setBrush(QBrush(QColor(80, 200, 120, 40)))
        self.setAcceptHoverEvents(True)

    def _handle_rects(self) -> dict:
        r, h = self.rect(), self.handle_px
        cx, cy = r.center().x(), r.center().y()
        pts = {"tl": (r.left(), r.top()), "t": (cx, r.top()), "tr": (r.right(), r.top()),
               "r": (r.right(), cy), "br": (r.right(), r.bottom()), "b": (cx, r.bottom()),
               "bl": (r.left(), r.bottom()), "l": (r.left(), cy)}
        return {k: QRectF(x - h, y - h, 2 * h, 2 * h) for k, (x, y) in pts.items()}

    def _handle_at(self, pos: QPointF) -> Optional[str]:
        for name, hr in self._handle_rects().items():
            if hr.contains(pos):
                return name
        return None

    def boundingRect(self) -> QRectF:
        return self.rect().adjusted(-self.handle_px, -self.handle_px,
                                    self.handle_px, self.handle_px)

    def paint(self, painter, option, widget=None):
        super().paint(painter, option, widget)
        painter.setBrush(QBrush(QColor(80, 200, 120)))
        painter.setPen(QPen(QColor(20, 60, 30), 0))
        for hr in self._handle_rects().values():
            painter.drawRect(hr)

    def mousePressEvent(self, event):
        self._handle = self._handle_at(event.pos())
        self._press = event.pos()
        self._press_rect = QRectF(self.rect())

    def mouseMoveEvent(self, event):
        delta = event.pos() - self._press
        r = QRectF(self._press_rect)
        if self._handle is None:
            r.translate(delta)
            self._clamp_move(r)
        else:
            left, top, right, bottom = r.getCoords()
            edges = _EDGES[self._handle]
            if "l" in edges:
                left += delta.x()
            if "r" in edges:
                right += delta.x()
            if "t" in edges:
                top += delta.y()
            if "b" in edges:
                bottom += delta.y()
            r = QRectF()
            r.setCoords(left, top, right, bottom)
            r = r.normalized()
            self._clamp_resize(r)
        self.setRect(r)
        self.on_change(r)

    def _clamp_move(self, r: QRectF) -> None:
        if r.left() < self.bounds.left():
            r.moveLeft(self.bounds.left())
        if r.top() < self.bounds.top():
            r.moveTop(self.bounds.top())
        if r.right() > self.bounds.right():
            r.moveRight(self.bounds.right())
        if r.bottom() > self.bounds.bottom():
            r.moveBottom(self.bounds.bottom())

    def _clamp_resize(self, r: QRectF) -> None:
        r.setLeft(max(self.bounds.left(), min(r.left(), r.right() - 4)))
        r.setTop(max(self.bounds.top(), min(r.top(), r.bottom() - 4)))
        r.setRight(min(self.bounds.right(), max(r.right(), r.left() + 4)))
        r.setBottom(min(self.bounds.bottom(), max(r.bottom(), r.top() + 4)))

    def set_rect_external(self, r: QRectF) -> None:
        self.prepareGeometryChange()
        self.setRect(r)


class CropWidget(QWidget):
    def __init__(self):
        super().__init__()
        self._src: Optional[str] = None
        self._img_size = (0, 0)
        self._syncing = False
        self._build()

    def _build(self) -> None:
        self.scene = QGraphicsScene()
        self.view = QGraphicsView(self.scene)
        self.view.setMinimumSize(420, 420)
        self.pix_item = None
        self.crop_item: Optional[CropRectItem] = None

        # crop fields
        self.sx, self.sy, self.sw, self.sh = (QSpinBox() for _ in range(4))
        for sb in (self.sx, self.sy, self.sw, self.sh):
            sb.setMaximum(100000)
            sb.valueChanged.connect(self._fields_to_rect)
        crop_box = QGroupBox("Crop (source px)")
        cf = QFormLayout(crop_box)
        cf.addRow("x", self.sx); cf.addRow("y", self.sy)
        cf.addRow("w", self.sw); cf.addRow("h", self.sh)

        # framing fields
        self.cw, self.ch = QSpinBox(), QSpinBox()
        for sb in (self.cw, self.ch):
            sb.setRange(16, 8192); sb.setValue(1254)
        self.char_frac = QDoubleSpinBox()
        self.char_frac.setRange(0.05, 1.0); self.char_frac.setSingleStep(0.05)
        self.char_frac.setValue(0.65)
        self.ground = QSpinBox(); self.ground.setRange(0, 8192); self.ground.setValue(1180)
        self.char_x = QDoubleSpinBox()
        self.char_x.setRange(0.0, 1.0); self.char_x.setSingleStep(0.05); self.char_x.setValue(0.5)
        frame_box = QGroupBox("Framing (output keypose)")
        ff = QFormLayout(frame_box)
        ff.addRow("canvas w", self.cw); ff.addRow("canvas h", self.ch)
        ff.addRow("char height %", self.char_frac)
        ff.addRow("ground y", self.ground); ff.addRow("char x", self.char_x)

        self.preview_btn = QPushButton("Preview normalized")
        self.preview_btn.clicked.connect(self.update_preview)
        self.preview = QLabel("(no preview)")
        self.preview.setMinimumSize(220, 220)
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setStyleSheet("background:#222;border:1px solid #444;")

        controls = QVBoxLayout()
        controls.addWidget(crop_box)
        controls.addWidget(frame_box)
        controls.addWidget(self.preview_btn)
        controls.addWidget(self.preview)
        controls.addStretch(1)

        lay = QHBoxLayout(self)
        lay.addWidget(self.view, 2)
        right = QWidget(); right.setLayout(controls)
        lay.addWidget(right, 1)

    # ---- source ---------------------------------------------------------
    def set_source(self, path: str) -> None:
        self._src = path
        pm = QPixmap(path)
        self._img_size = (pm.width(), pm.height())
        self.scene.clear()
        self.pix_item = self.scene.addPixmap(pm)
        self.scene.setSceneRect(QRectF(pm.rect()))
        # default crop = full image
        bounds = QRectF(0, 0, pm.width(), pm.height())
        self.crop_item = CropRectItem(bounds, self._rect_to_fields)
        self.crop_item.setRect(bounds)
        self.scene.addItem(self.crop_item)
        self.view.fitInView(self.scene.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)
        for sb, mx in ((self.sx, pm.width()), (self.sy, pm.height()),
                       (self.sw, pm.width()), (self.sh, pm.height())):
            sb.setMaximum(max(1, mx))
        self._syncing = True
        self.sx.setValue(0); self.sy.setValue(0)
        self.sw.setValue(pm.width()); self.sh.setValue(pm.height())
        self.ground.setValue(round(self.ch.value() * 0.94))
        self._syncing = False

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.scene.sceneRect().isValid():
            self.view.fitInView(self.scene.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)

    # ---- two-way sync ---------------------------------------------------
    def _rect_to_fields(self, r: QRectF) -> None:
        if self._syncing:
            return
        self._syncing = True
        self.sx.setValue(round(r.left())); self.sy.setValue(round(r.top()))
        self.sw.setValue(round(r.width())); self.sh.setValue(round(r.height()))
        self._syncing = False

    def _fields_to_rect(self) -> None:
        if self._syncing or self.crop_item is None:
            return
        r = QRectF(self.sx.value(), self.sy.value(), self.sw.value(), self.sh.value())
        self.crop_item.set_rect_external(r)

    # ---- output ---------------------------------------------------------
    def get_crop(self) -> Optional[list]:
        if not self._src:
            return None
        return [self.sx.value(), self.sy.value(), self.sw.value(), self.sh.value()]

    def get_framing(self) -> dict:
        return {
            "crop": self.get_crop(),
            "canvas": [self.cw.value(), self.ch.value()],
            "char_height_frac": round(self.char_frac.value(), 3),
            "ground_y": self.ground.value(),
            "char_x": round(self.char_x.value(), 3),
        }

    def _normalize(self, out_path: Optional[str] = None) -> dict:
        f = self.get_framing()
        return framing.normalize_keypose(
            self._src, crop=tuple(f["crop"]) if f["crop"] else None,
            canvas=tuple(f["canvas"]), char_height_frac=f["char_height_frac"],
            ground_y=f["ground_y"], char_x=f["char_x"], out_path=out_path)

    def update_preview(self) -> None:
        if not self._src:
            return
        try:
            meta = self._normalize(None)
        except Exception as e:  # noqa: BLE001 - show the message in the preview slot
            self.preview.setText(f"preview error:\n{e}")
            return
        pm = pil_to_qpixmap(meta["image"]).scaled(
            self.preview.size(), Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation)
        self.preview.setPixmap(pm)

    def bake(self, out_path: str) -> dict:
        """Write the normalized keypose to out_path and return its metadata."""
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        return self._normalize(out_path)
