"""Render resources/icon.svg into a multi-size Windows icon.ico (and a few PNGs).

Vector source of truth is resources/icon.svg. The running app loads that SVG
directly via QIcon (resolution-independent); this script bakes a .ico for the
Windows taskbar / executable / file association, rendering each embedded size
straight from the vector (crisper at 16-32px than downscaling one big raster).

Run headless:
    QT_QPA_PLATFORM=offscreen PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe scripts/make_icon.py
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QByteArray, QRectF, Qt  # noqa: E402
from PySide6.QtGui import QImage, QPainter  # noqa: E402
from PySide6.QtSvg import QSvgRenderer  # noqa: E402
from PIL import Image  # noqa: E402

import paths  # noqa: E402

ICO_SIZES = [16, 24, 32, 48, 64, 128, 256]
PNG_SIZES = [256]


def _render(svg: QSvgRenderer, size: int) -> Image.Image:
    img = QImage(size, size, QImage.Format.Format_ARGB32)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
    svg.render(p, QRectF(0, 0, size, size))
    p.end()
    buf = QByteArray()
    from PySide6.QtCore import QBuffer
    qb = QBuffer(buf)
    qb.open(QBuffer.OpenModeFlag.WriteOnly)
    img.save(qb, "PNG")
    qb.close()
    return Image.open(io.BytesIO(bytes(buf))).convert("RGBA")


def main() -> int:
    svg_path = paths.APP_ICON_SVG
    if not svg_path.exists():
        print(f"missing source: {svg_path}", file=sys.stderr)
        return 1

    # QGuiApplication is required for QImage/QPainter to work off-screen.
    from PySide6.QtGui import QGuiApplication
    app = QGuiApplication.instance() or QGuiApplication(sys.argv)

    svg = QSvgRenderer(str(svg_path))
    frames = {s: _render(svg, s) for s in sorted(set(ICO_SIZES + PNG_SIZES))}

    paths.RESOURCES_DIR.mkdir(parents=True, exist_ok=True)
    largest = frames[max(ICO_SIZES)]
    largest.save(
        paths.APP_ICON_ICO,
        format="ICO",
        sizes=[(s, s) for s in ICO_SIZES],
        append_images=[frames[s] for s in ICO_SIZES if s != max(ICO_SIZES)],
    )
    print(f"wrote {paths.APP_ICON_ICO}  ({', '.join(str(s) for s in ICO_SIZES)} px)")

    for s in PNG_SIZES:
        out = paths.RESOURCES_DIR / f"icon_{s}.png"
        frames[s].save(out)
        print(f"wrote {out}")

    del app
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
