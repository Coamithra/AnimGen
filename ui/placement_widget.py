"""Placement canvas - drag/scale a keyed keyframe sprite onto the shot's aspect canvas.

Replaces the old crop-rect tool. Shows the magenta contract canvas at the shot's chosen
aspect ratio; the keyed sprite is a movable item framed by a classic transform box - a
dashed outline with corner handles. Drag the body to move; drag a corner to scale
(uniform, since placement stores a single scale). Placement is reported normalized so it
survives aspect changes:
    {scale: sprite-height / canvas-height, cx, cy: sprite center as 0..1 of the canvas}.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, Qt, Signal
from PySide6.QtGui import (
    QBrush, QColor, QImage, QPainter, QPen, QPixmap, QPolygon,
)
from PySide6.QtWidgets import (
    QAbstractSpinBox, QDoubleSpinBox, QGraphicsItem, QGraphicsPixmapItem,
    QGraphicsRectItem, QGraphicsScene, QGraphicsView, QGridLayout, QHBoxLayout,
    QLabel, QWidget,
)

MAGENTA = QColor(255, 0, 255)
_DEFAULT_CY = 0.6      # feet a bit below center by default
_DEFAULT_SCALE = 0.65  # sprite height as fraction of canvas height
_MIN_NORM = 0.05
_MAX_NORM = 1.5
_FIT_PAD = 0.12        # extra breathing room so handles just outside the canvas stay reachable

_HANDLE = 5            # half-size of a drawn handle, in viewport px
_GRAB = 9             # half-size of a handle's hit area, in viewport px

# corner indices: 0 top-left, 1 top-right, 2 bottom-right, 3 bottom-left
_DIAG_CURSOR = {0: Qt.CursorShape.SizeFDiagCursor, 2: Qt.CursorShape.SizeFDiagCursor,
                1: Qt.CursorShape.SizeBDiagCursor, 3: Qt.CursorShape.SizeBDiagCursor}


def pil_to_pixmap(im) -> QPixmap:
    """PIL RGBA -> QPixmap (preserves alpha)."""
    im = im.convert("RGBA")
    qimg = QImage(im.tobytes("raw", "RGBA"), im.width, im.height, im.width * 4,
                  QImage.Format.Format_RGBA8888)
    return QPixmap.fromImage(qimg.copy())


class _SpriteItem(QGraphicsPixmapItem):
    """The keyed sprite. Plain pixmap item; the view drives all interaction.

    Parented to the canvas rect (which clips children to its shape) so the part
    that hangs off the frame is not drawn - while the view's overlay still draws
    the full dashed box and handles in viewport space.
    """

    def __init__(self, pixmap: QPixmap, parent: Optional[QGraphicsItem] = None):
        super().__init__(pixmap, parent)
        self.setTransformationMode(Qt.TransformationMode.SmoothTransformation)


class _PlacementView(QGraphicsView):
    """Graphics view that draws a transform box over the sprite and handles
    move/scale interaction (corner handles scale uniformly; body drags)."""

    placementChanged = Signal()   # committed (on release) - marks the shot dirty
    geometryChanged = Signal()    # live during a drag/scale - for the readout panel

    def __init__(self, scene: QGraphicsScene):
        super().__init__(scene)
        self.setMinimumSize(360, 360)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setMouseTracking(True)
        self.sprite_item: Optional[_SpriteItem] = None
        self._mode: Optional[str] = None   # 'move' | 'scale'
        self._corner = -1
        self._anchor = QPointF()           # fixed (opposite) corner, scene coords
        self._orig_corner = QPointF()      # dragged corner at press, scene coords
        self._orig_scale = 1.0
        self._press_scene = QPointF()
        self._press_pos = QPointF()        # sprite pos at press

    def set_sprite(self, item: Optional[_SpriteItem]) -> None:
        self.sprite_item = item
        self._mode = None
        self.viewport().update()

    # ---- geometry helpers ----------------------------------------------
    def _scene_rect(self) -> QRectF:
        assert self.sprite_item is not None
        return self.sprite_item.sceneBoundingRect()

    def _scene_corners(self) -> list[QPointF]:
        r = self._scene_rect()
        return [r.topLeft(), r.topRight(), r.bottomRight(), r.bottomLeft()]

    def _viewport_corners(self) -> list[QPoint]:
        return [self.mapFromScene(p) for p in self._scene_corners()]

    def _hit_corner(self, vp_pos: QPoint) -> int:
        for i, c in enumerate(self._viewport_corners()):
            if QRect(c.x() - _GRAB, c.y() - _GRAB, 2 * _GRAB, 2 * _GRAB).contains(vp_pos):
                return i
        return -1

    def _clamp_pos(self, pos: QPointF) -> QPointF:
        """Keep the sprite's center inside the canvas so it can always be grabbed.
        The sprite may still hang off any edge by up to half its size."""
        assert self.sprite_item is not None
        w = self.sprite_item.pixmap().width() * self.sprite_item.scale()
        h = self.sprite_item.pixmap().height() * self.sprite_item.scale()
        canvas = self.scene().sceneRect()
        cx = min(max(pos.x() + w / 2, canvas.left()), canvas.right())
        cy = min(max(pos.y() + h / 2, canvas.top()), canvas.bottom())
        return QPointF(cx - w / 2, cy - h / 2)

    def _clamp_pscale(self, pscale: float) -> float:
        """Clamp a pixmap-item scale so normalized scale stays in [_MIN, _MAX]."""
        assert self.sprite_item is not None
        native_h = self.sprite_item.pixmap().height()
        canvas_h = self.scene().sceneRect().height()
        if not native_h or not canvas_h:
            return pscale
        norm = pscale * native_h / canvas_h
        norm = max(_MIN_NORM, min(_MAX_NORM, norm))
        return norm * canvas_h / native_h

    def _apply_scale_anchored(self, pscale: float) -> None:
        """Scale the sprite, keeping the anchor corner fixed in scene coords."""
        item = self.sprite_item
        assert item is not None
        nw = item.pixmap().width() * pscale
        nh = item.pixmap().height() * pscale
        a, i = self._anchor, self._corner
        left = a.x() if i in (1, 2) else a.x() - nw   # anchor on the left edge for right-side drags
        top = a.y() if i in (2, 3) else a.y() - nh    # anchor on the top edge for bottom-side drags
        item.setScale(pscale)
        item.setPos(left, top)

    # ---- painting -------------------------------------------------------
    def paintEvent(self, event):  # noqa: N802 - Qt override
        super().paintEvent(event)
        if not self.sprite_item:
            return
        corners = self._viewport_corners()
        poly = QPolygon(corners)
        p = QPainter(self.viewport())
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        # dashed outline: a dark underlay then a white dash for contrast on magenta + sprite
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor(0, 0, 0, 150), 1, Qt.PenStyle.SolidLine))
        p.drawPolygon(poly)
        dash = QPen(QColor(255, 255, 255), 1, Qt.PenStyle.DashLine)
        p.setPen(dash)
        p.drawPolygon(poly)
        # corner handles
        for c in corners:
            r = QRect(c.x() - _HANDLE, c.y() - _HANDLE, 2 * _HANDLE, 2 * _HANDLE)
            p.fillRect(r, QColor(255, 255, 255))
            p.setPen(QPen(QColor(60, 60, 60), 1))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(r)
        p.end()

    # ---- interaction ----------------------------------------------------
    def mousePressEvent(self, event):  # noqa: N802 - Qt override
        if self.sprite_item and event.button() == Qt.MouseButton.LeftButton:
            vp = event.position().toPoint()
            i = self._hit_corner(vp)
            if i >= 0:
                corners = self._scene_corners()
                self._mode = "scale"
                self._corner = i
                self._anchor = corners[(i + 2) % 4]
                self._orig_corner = corners[i]
                self._orig_scale = self.sprite_item.scale()
                event.accept()
                return
            scene_pt = self.mapToScene(vp)
            if self._scene_rect().contains(scene_pt):
                self._mode = "move"
                self._press_scene = scene_pt
                self._press_pos = self.sprite_item.pos()
                self.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):  # noqa: N802 - Qt override
        vp = event.position().toPoint()
        if self._mode == "move" and self.sprite_item:
            delta = self.mapToScene(vp) - self._press_scene
            self.sprite_item.setPos(self._clamp_pos(self._press_pos + delta))
            self.geometryChanged.emit()
            self.viewport().update()
            event.accept()
            return
        if self._mode == "scale":
            ov = self._orig_corner - self._anchor
            nv = self.mapToScene(vp) - self._anchor
            denom = ov.x() * ov.x() + ov.y() * ov.y()
            factor = ((nv.x() * ov.x() + nv.y() * ov.y()) / denom) if denom else 1.0
            self._apply_scale_anchored(self._clamp_pscale(self._orig_scale * factor))
            self.geometryChanged.emit()
            self.viewport().update()
            event.accept()
            return
        self._update_hover_cursor(vp)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):  # noqa: N802 - Qt override
        if self._mode in ("move", "scale"):
            self._mode = None
            self._update_hover_cursor(event.position().toPoint())
            self.placementChanged.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def _update_hover_cursor(self, vp: QPoint) -> None:
        if not self.sprite_item:
            self.viewport().unsetCursor()
            return
        i = self._hit_corner(vp)
        if i >= 0:
            self.viewport().setCursor(_DIAG_CURSOR[i])
        elif self._scene_rect().contains(self.mapToScene(vp)):
            self.viewport().setCursor(Qt.CursorShape.OpenHandCursor)
        else:
            self.viewport().unsetCursor()


class PlacementCanvas(QWidget):
    changed = Signal()

    def __init__(self):
        super().__init__()
        self._w = self._h = 1254
        self._native: Optional[QPixmap] = None
        self.sprite_item: Optional[_SpriteItem] = None
        self._refreshing = False   # guards readback from re-triggering edit handlers
        self._build()

    def _build(self) -> None:
        self.scene = QGraphicsScene()
        self.view = _PlacementView(self.scene)
        self.view.placementChanged.connect(self.changed)
        self.view.geometryChanged.connect(self._update_info)
        self.canvas_item = QGraphicsRectItem(0, 0, self._w, self._h)
        self.canvas_item.setBrush(QBrush(MAGENTA))
        self.canvas_item.setPen(QPen(QColor("#555555"), 0))
        self.canvas_item.setZValue(-1)
        # clip children (the sprite) to the canvas so off-frame parts aren't drawn
        self.canvas_item.setFlag(QGraphicsItem.GraphicsItemFlag.ItemClipsChildrenToShape, True)
        self.scene.addItem(self.canvas_item)
        self.scene.setSceneRect(0, 0, self._w, self._h)

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(self.view, 1)
        root.addWidget(self._build_info_panel())
        self._update_info()

    def _build_info_panel(self) -> QWidget:
        panel = QWidget()
        panel.setMaximumWidth(190)
        g = QGridLayout(panel)
        g.setContentsMargins(10, 0, 0, 0)
        g.setVerticalSpacing(3)
        g.setHorizontalSpacing(8)

        def header(text: str) -> QLabel:
            lbl = QLabel(text)
            lbl.setStyleSheet("font-weight: bold;")
            return lbl

        def spin(suffix: str, lo: float, hi: float) -> QDoubleSpinBox:
            sb = QDoubleSpinBox()
            sb.setDecimals(0)
            sb.setRange(lo, hi)
            sb.setSingleStep(1)
            sb.setSuffix(suffix)
            sb.setKeyboardTracking(False)   # emit valueChanged on commit, not per keystroke
            sb.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.UpDownArrows)
            sb.setAlignment(Qt.AlignmentFlag.AlignRight)
            return sb

        # Position is the sprite's top-left in canvas px; size is canvas px and a
        # % of the asset's native resolution. W/H/% are linked views of the single
        # uniform scale, so editing any one drives the others.
        self.x_box = spin(" px", -99999, 99999)
        self.y_box = spin(" px", -99999, 99999)
        self.w_box = spin(" px", 0, 99999)
        self.h_box = spin(" px", 0, 99999)
        self.w_pct_box = spin(" %", 0, 9999)
        self.h_pct_box = spin(" %", 0, 9999)
        for box in (self.w_pct_box, self.h_pct_box):
            box.setStyleSheet("color: gray;")

        self.x_box.valueChanged.connect(self._on_pos_edit)
        self.y_box.valueChanged.connect(self._on_pos_edit)
        self.w_box.valueChanged.connect(lambda *_: self._on_size_edit("w_px"))
        self.h_box.valueChanged.connect(lambda *_: self._on_size_edit("h_px"))
        self.w_pct_box.valueChanged.connect(lambda *_: self._on_size_edit("w_pct"))
        self.h_pct_box.valueChanged.connect(lambda *_: self._on_size_edit("h_pct"))

        r = 0
        g.addWidget(header("Position"), r, 0, 1, 2); r += 1
        g.addWidget(QLabel("X"), r, 0); g.addWidget(self.x_box, r, 1); r += 1
        g.addWidget(QLabel("Y"), r, 0); g.addWidget(self.y_box, r, 1); r += 1
        g.addWidget(QLabel(""), r, 0); r += 1
        g.addWidget(header("Size"), r, 0, 1, 2); r += 1
        g.addWidget(QLabel("W"), r, 0); g.addWidget(self.w_box, r, 1); r += 1
        g.addWidget(QLabel("W %"), r, 0); g.addWidget(self.w_pct_box, r, 1); r += 1
        g.addWidget(QLabel("H"), r, 0); g.addWidget(self.h_box, r, 1); r += 1
        g.addWidget(QLabel("H %"), r, 0); g.addWidget(self.h_pct_box, r, 1); r += 1
        g.setColumnStretch(1, 1)
        g.setRowStretch(r, 1)
        return panel

    @property
    def _boxes(self) -> tuple:
        return (self.x_box, self.y_box, self.w_box, self.h_box,
                self.w_pct_box, self.h_pct_box)

    def _update_info(self) -> None:
        """Refresh the readout boxes: sprite position + size in canvas pixels, with
        the size as a % of the raw input asset's native resolution. Guarded so the
        programmatic setValue doesn't re-fire the edit handlers."""
        sprite, native = self.sprite_item, self._native
        has = bool(sprite and native and native.width() and native.height())
        self._refreshing = True
        try:
            for box in self._boxes:
                box.setEnabled(has)
            if not (sprite and native and native.width() and native.height()):
                return
            s = sprite.scale()
            pos = sprite.pos()
            cw = native.width() * s
            ch = native.height() * s
            self.x_box.setValue(pos.x())
            self.y_box.setValue(pos.y())
            self.w_box.setValue(cw)
            self.h_box.setValue(ch)
            self.w_pct_box.setValue(cw / native.width() * 100)
            self.h_pct_box.setValue(ch / native.height() * 100)
        finally:
            self._refreshing = False

    # ---- numeric editing ------------------------------------------------
    def _on_pos_edit(self) -> None:
        if self._refreshing or not (self.sprite_item and self._native):
            return
        self.sprite_item.setPos(
            self.view._clamp_pos(QPointF(self.x_box.value(), self.y_box.value())))
        self.view.viewport().update()
        self.changed.emit()
        self._update_info()

    def _on_size_edit(self, source: str) -> None:
        if self._refreshing or not (self.sprite_item and self._native
                                    and self._native.width() and self._native.height()):
            return
        if source == "w_px":
            s = self.w_box.value() / self._native.width()
        elif source == "h_px":
            s = self.h_box.value() / self._native.height()
        elif source == "w_pct":
            s = self.w_pct_box.value() / 100.0
        else:
            s = self.h_pct_box.value() / 100.0
        cx, cy = self._center()                          # anchor about the center
        self._apply_norm(s * self._native.height() / self._h)   # clamps norm to [_MIN, _MAX]
        self._place_center(cx, cy)
        self.changed.emit()
        self._update_info()

    # ---- configuration --------------------------------------------------
    def set_aspect(self, w: int, h: int) -> None:
        cx, cy = self._center()
        norm = self._current_norm()           # capture under the OLD canvas height
        self._w, self._h = int(w), int(h)
        self.canvas_item.setRect(0, 0, self._w, self._h)
        self.scene.setSceneRect(0, 0, self._w, self._h)
        self._fit()
        if self.sprite_item:
            self._apply_norm(norm)
            self._place_center(cx, cy)
        self.view.viewport().update()
        self._update_info()

    def set_sprite(self, pixmap: Optional[QPixmap]) -> None:
        if self.sprite_item:
            self.scene.removeItem(self.sprite_item)
            self.sprite_item = None
            self.view.set_sprite(None)
        self._native = pixmap if (pixmap and not pixmap.isNull()) else None
        if self._native:
            self.sprite_item = _SpriteItem(self._native, parent=self.canvas_item)
            self._apply_norm(_DEFAULT_SCALE)
            self._place_center(0.5, _DEFAULT_CY)
            self.view.set_sprite(self.sprite_item)
        self.view.viewport().update()
        self._update_info()

    def set_placement(self, p: dict) -> None:
        if self.sprite_item:
            self._apply_norm(float(p.get("scale", _DEFAULT_SCALE)))
            self._place_center(float(p.get("cx", 0.5)), float(p.get("cy", _DEFAULT_CY)))
        self.view.viewport().update()
        self._update_info()

    def get_placement(self) -> dict:
        cx, cy = self._center()
        return {"scale": round(self._current_norm(), 4), "cx": round(cx, 4), "cy": round(cy, 4)}

    # ---- internals ------------------------------------------------------
    def _fit(self) -> None:
        r = self.canvas_item.rect()
        m = _FIT_PAD * max(r.width(), r.height())
        self.view.fitInView(r.adjusted(-m, -m, m, m), Qt.AspectRatioMode.KeepAspectRatio)

    def resizeEvent(self, event):  # noqa: N802 - Qt override
        super().resizeEvent(event)
        self._fit()

    def _current_norm(self) -> float:
        if self.sprite_item and self._native and self._h:
            return (self.sprite_item.scale() * self._native.height()) / self._h
        return _DEFAULT_SCALE

    def _apply_norm(self, norm: float) -> None:
        if self.sprite_item and self._native and self._native.height():
            norm = max(_MIN_NORM, min(_MAX_NORM, norm))
            self.sprite_item.setScale((norm * self._h) / self._native.height())

    def _center(self) -> tuple[float, float]:
        if not (self.sprite_item and self._native):
            return 0.5, _DEFAULT_CY
        s = self.sprite_item.scale()
        pos = self.sprite_item.pos()
        return ((pos.x() + self._native.width() * s / 2) / self._w,
                (pos.y() + self._native.height() * s / 2) / self._h)

    def _place_center(self, cx: float, cy: float) -> None:
        if not (self.sprite_item and self._native):
            return
        s = self.sprite_item.scale()
        self.sprite_item.setPos(cx * self._w - self._native.width() * s / 2,
                                cy * self._h - self._native.height() * s / 2)
        self.view.viewport().update()
