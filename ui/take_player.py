"""TakePlayerTab - an in-app frame-by-frame viewer for one generated take.

Double-clicking a take opens it here (a tab in the main window) instead of launching an
external movie player, so the take can be inspected frame-by-frame - the whole point of a
2D-animation tool. Controls: play / pause / stop, a seek slider, and prev/next-frame step
buttons, plus a "frame N / M" readout.

Frames are decoded once, off the GUI thread, into QImages (extract.iter_frames via PyAV,
which handles both the real .mp4 renders and the .gif retime previews). The take's measured
fps drives playback; QPixmaps are built lazily from the QImages and cached as frames are
shown. These clips are short (a second or two of game animation), so holding the decoded
frames in memory is cheap and gives instant scrubbing in both directions.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QPushButton, QSlider, QVBoxLayout, QWidget,
)

from store.project import Project
from ui.placement_widget import pil_to_qimage

_MAX_FRAMES = 1200      # safety cap so a pathologically long clip can't exhaust memory
_DEFAULT_FPS = 12.0


def take_source(take) -> Optional[str]:
    """The best playable file for a take: the real render if present, else the gif preview.
    Returns None if neither exists on disk (e.g. a still-pending or failed take)."""
    for cand in (take.video_path, take.preview_gif):
        if cand and Path(cand).exists():
            return cand
    return None


class _FrameLoader(QObject):
    """Decodes a clip to QImages on a daemon thread, then hands them back on the GUI thread.

    QImage (unlike QPixmap) is safe to construct off the GUI thread, so the worker produces
    QImages and the tab converts them to QPixmaps lazily as each frame is displayed."""
    done = Signal(list, float)     # list[QImage], fps
    failed = Signal(str)

    def __init__(self, source: str):
        super().__init__()
        self._source = source

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        try:
            from pipeline import extract
            fps = extract.video_info(self._source).get("fps") or _DEFAULT_FPS
            frames = []
            for i, im in enumerate(extract.iter_frames(self._source)):
                if i >= _MAX_FRAMES:
                    break
                frames.append(pil_to_qimage(im))
            if not frames:
                self.failed.emit("no frames could be decoded")
                return
            self.done.emit(frames, float(fps))
        except Exception as e:  # noqa: BLE001 - surface any decode failure in the tab
            self.failed.emit(f"{type(e).__name__}: {e}")


class TakePlayerTab(QWidget):
    def __init__(self, project: Project, take_id: str, parent=None):
        super().__init__(parent)
        self.project = project
        self.take_id = take_id
        self._frames: list[QImage] = []
        self._pix_cache: dict[int, QPixmap] = {}
        self._idx = 0
        self._fps = _DEFAULT_FPS
        self._loader: Optional[_FrameLoader] = None
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._advance)
        self._build()
        self._load()

    # ---- build ----------------------------------------------------------
    def _build(self) -> None:
        self.canvas = QLabel("Decoding frames…")
        self.canvas.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.canvas.setMinimumSize(320, 240)
        self.canvas.setStyleSheet("background:#111; color:#888;")

        self.prev_btn = QPushButton("⏮ Prev")
        self.play_btn = QPushButton("▶ Play")
        self.stop_btn = QPushButton("⏹ Stop")
        self.next_btn = QPushButton("Next ⏭")
        self.prev_btn.clicked.connect(self.prev_frame)
        self.play_btn.clicked.connect(self.toggle_play)
        self.stop_btn.clicked.connect(self.stop)
        self.next_btn.clicked.connect(self.next_frame)

        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setEnabled(False)
        self.slider.valueChanged.connect(self._on_slider)

        self.frame_label = QLabel("- / -")
        self.frame_label.setMinimumWidth(96)
        self.frame_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        for b in (self.prev_btn, self.play_btn, self.stop_btn, self.next_btn):
            b.setEnabled(False)

        controls = QHBoxLayout()
        controls.addWidget(self.prev_btn)
        controls.addWidget(self.play_btn)
        controls.addWidget(self.stop_btn)
        controls.addWidget(self.next_btn)
        controls.addWidget(self.slider, 1)
        controls.addWidget(self.frame_label)

        lay = QVBoxLayout(self)
        lay.addWidget(self.canvas, 1)
        lay.addLayout(controls)

    # ---- load -----------------------------------------------------------
    def _load(self) -> None:
        take = self.project.get_take(self.take_id)
        source = take_source(take) if take else None
        if not source:
            self.canvas.setText("This take has no playable video yet.")
            return
        self._loader = _FrameLoader(source)        # kept on self so it isn't GC'd mid-decode
        self._loader.done.connect(self._on_loaded)
        self._loader.failed.connect(self._on_failed)
        self._loader.start()

    def _on_loaded(self, frames: list, fps: float) -> None:
        self._frames = frames
        self._fps = fps if fps and fps > 0 else _DEFAULT_FPS
        self.slider.setRange(0, len(frames) - 1)
        self.slider.setEnabled(True)
        for b in (self.prev_btn, self.play_btn, self.stop_btn, self.next_btn):
            b.setEnabled(True)
        self._show(0)
        self.toggle_play()                          # auto-play once decoded, like a preview

    def _on_failed(self, msg: str) -> None:
        self.canvas.setText(f"Could not load this take:\n{msg}")

    # ---- playback -------------------------------------------------------
    def toggle_play(self) -> None:
        if not self._frames:
            return
        if self._timer.isActive():
            self._timer.stop()
            self.play_btn.setText("▶ Play")
        else:
            self._timer.start(max(1, round(1000 / self._fps)))
            self.play_btn.setText("⏸ Pause")

    def _pause(self) -> None:
        if self._timer.isActive():
            self._timer.stop()
            self.play_btn.setText("▶ Play")

    def stop(self) -> None:
        self._pause()
        self._show(0)

    def _advance(self) -> None:
        if self._frames:
            self._show((self._idx + 1) % len(self._frames))   # loop

    def next_frame(self) -> None:
        self._pause()
        if self._frames:
            self._show(min(self._idx + 1, len(self._frames) - 1))

    def prev_frame(self) -> None:
        self._pause()
        if self._frames:
            self._show(max(self._idx - 1, 0))

    def _on_slider(self, value: int) -> None:
        if value != self._idx:
            self._pause()
            self._show(value)

    # ---- display --------------------------------------------------------
    def _pixmap(self, idx: int) -> QPixmap:
        pm = self._pix_cache.get(idx)
        if pm is None:
            pm = QPixmap.fromImage(self._frames[idx])
            self._pix_cache[idx] = pm
        return pm

    def _show(self, idx: int) -> None:
        if not self._frames:
            return
        self._idx = idx
        pm = self._pixmap(idx).scaled(
            self.canvas.size(), Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation)
        self.canvas.setPixmap(pm)
        self.frame_label.setText(f"{idx + 1} / {len(self._frames)}")
        if self.slider.value() != idx:
            self.slider.blockSignals(True)          # programmatic move, not a user seek
            self.slider.setValue(idx)
            self.slider.blockSignals(False)

    def resizeEvent(self, event):  # noqa: N802 - Qt override
        super().resizeEvent(event)
        if self._frames:
            self._show(self._idx)                   # rescale current frame to the new size

    def close_player(self) -> None:
        """Stop playback before the tab is removed (called by MainWindow on tab close)."""
        self._timer.stop()
