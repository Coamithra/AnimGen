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

import tempfile
import threading
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import QMimeData, QObject, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QImage, QKeySequence, QPixmap
from PySide6.QtWidgets import (
    QApplication, QDockWidget, QFileDialog, QHBoxLayout, QLabel, QMainWindow, QMenu,
    QMessageBox, QPushButton, QSlider, QTextEdit, QVBoxLayout, QWidget,
)

import library
from qt_guard import guarded_emit
from store.models import STATUS_CANCELLED, STATUS_FAILED
from store.project import Project
from ui.placement_widget import pil_to_qimage

_MAX_FRAMES = 1200      # safety cap so a pathologically long clip can't exhaust memory
_DEFAULT_FPS = 12.0


def format_generation_settings(take, shot=None) -> str:
    """Render a take's immutable settings_snapshot as ordered, human-readable text.

    Pure / Qt-free so it can be unit-tested headless. Degrades gracefully when the snapshot
    is sparse (older takes recorded fewer fields) or absent. `shot` is accepted for parity
    with other take views but isn't needed - the snapshot is the authoritative provenance."""
    snap = (take.settings_snapshot or {}) if take else {}
    if not snap:
        return "No generation settings were recorded for this take."

    model_id = snap.get("model_id", "")
    model = library.get_model(model_id) if model_id else None
    lines = [f"Model:     {model['display_name'] if model else (model_id or '?')}"]
    backend = snap.get("backend") or (model.get("backend") if model else "")
    if backend:
        lines.append(f"Backend:   {backend}")
    if snap.get("replicate_model_id"):
        lines.append(f"Replicate: {snap['replicate_model_id']}")
    if snap.get("workflow_template"):
        lines.append(f"Workflow:  {snap['workflow_template']}")

    canvas = snap.get("canvas") or []
    if len(canvas) >= 2 and any(canvas):
        lines.append(f"Canvas:    {canvas[0]} x {canvas[1]}")
    aspect = (snap.get("crop") or {}).get("aspect")
    if aspect:
        lines.append(f"Aspect:    {aspect}")

    settings = dict(snap.get("settings") or {})
    seed = settings.pop("seed", None)
    if seed is None and take is not None:
        seed = take.seed
    if seed is not None:
        lines.append(f"Seed:      {seed}")

    lines.append("")
    lines.append("Prompt:")
    lines.append(snap.get("prompt") or "(none)")
    neg = snap.get("negative_prompt")
    if neg:
        lines += ["", "Negative prompt:", neg]

    if settings:
        lines.append("")
        lines.append("Parameters:")
        lines += [f"  {k}: {settings[k]}" for k in sorted(settings)]
    return "\n".join(lines)


def take_source(take) -> Optional[str]:
    """The best playable file for a take: the real render if present, else the gif preview.
    Returns None if neither exists on disk (e.g. a still-pending or failed take)."""
    for cand in (take.video_path, take.preview_gif):
        if cand and Path(cand).exists():
            return cand
    return None


def failure_message(take) -> Optional[str]:
    """Why a take with no playable video has nothing to show: the recorded backend error for
    a FAILED take, a short note for a CANCELLED one, else None (it's just still pending /
    generating, or there's no take). Pure / Qt-free so it's headless-testable; the viewer
    shows this on the canvas in place of the video."""
    if take is None:
        return None
    status = getattr(take, "status", "")
    if status == STATUS_FAILED:
        err = (getattr(take, "error", None) or "").strip()
        head = "This take failed to generate."
        return f"{head}\n\n{err}" if err else f"{head}\n\n(No error detail was recorded.)"
    if status == STATUS_CANCELLED:
        if getattr(take, "interrupted", False):
            return "This take was interrupted before it finished — it can be restarted."
        return "This take was cancelled before it started generating."
    return None


def decode_strip(source: str, max_side: int = 256, max_frames: int = 48,
                 raw_cap: int = 600) -> list:
    """Decode a clip to a list of small QImages for an animated grid thumbnail.

    Unlike QMovie (gif-only), this goes through PyAV, so it animates the real .mp4 renders
    too - which is what actually matters, since we don't generate gif previews. Each frame
    is downscaled to `max_side`, then the strip is sampled down to at most `max_frames` so a
    long clip stays light (the grid loop only needs to read as motion, not be frame-exact).
    Runs off the GUI thread: QImage is safe to build there; the caller turns them into
    QPixmaps on the GUI thread."""
    from pipeline import extract
    small = []
    for i, im in enumerate(extract.iter_frames(source)):
        if i >= raw_cap:
            break
        im = im.convert("RGBA")
        im.thumbnail((max_side, max_side))
        small.append(pil_to_qimage(im))
    if len(small) > max_frames:                       # sample evenly down to the cap
        step = len(small) / max_frames
        small = [small[int(k * step)] for k in range(max_frames)]
    return small


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
                # Guard the emit (card #48): _run is a daemon thread; a torn-down tab would make
                # a raw emit raise 'Signal source has been deleted' and abort the process.
                guarded_emit(self, "failed", "no frames could be decoded")
                return
            guarded_emit(self, "done", frames, float(fps))
        except Exception as e:  # noqa: BLE001 - surface any decode failure in the tab
            guarded_emit(self, "failed", f"{type(e).__name__}: {e}")


class _GifExporter(QObject):
    """Encodes a take's video to an animated GIF on a daemon thread (decode is PyAV-bound, so
    keep it off the GUI thread), then reports the output path back on the GUI thread."""
    done = Signal(str)         # output gif path
    failed = Signal(str)

    def __init__(self, source: str, out_path: str):
        super().__init__()
        self._source = source
        self._out = out_path

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        try:
            from pipeline import gif_export
            path = gif_export.take_to_gif(self._source, self._out)
            # Guard the emit (card #48): daemon thread; a torn-down owner would make a raw emit
            # raise 'Signal source has been deleted' and abort the process.
            guarded_emit(self, "done", str(path))
        except Exception as e:  # noqa: BLE001 - surface any encode failure in a dialog
            guarded_emit(self, "failed", f"{type(e).__name__}: {e}")


def star_button_text(starred: bool) -> str:
    """Label for the player's star toggle given the take's current star state. Pure so it's
    unit-testable headlessly; the filled star reads as "starred", the outline as "not"."""
    return "★ Starred" if starred else "☆ Star"


class TakePlayerTab(QWidget):
    star_changed = Signal(str)   # take_id -> the take's star was toggled here (MainWindow refreshes its grid tile)

    def __init__(self, project: Project, take_id: str, parent=None):
        super().__init__(parent)
        self.project = project
        self.take_id = take_id
        self._frames: list[QImage] = []
        self._pix_cache: dict[int, QPixmap] = {}
        self._idx = 0
        self._fps = _DEFAULT_FPS
        self._loader: Optional[_FrameLoader] = None
        self._gif_worker: Optional[_GifExporter] = None   # kept alive across the export
        self._gif_busy = False                            # one export at a time (GUI-thread flag)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._advance)
        self._build()
        self._load()

    # ---- build ----------------------------------------------------------
    def _build(self) -> None:
        self.canvas = QLabel("Decoding frames…")
        self.canvas.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.canvas.setMinimumSize(320, 240)
        self.canvas.setWordWrap(True)          # so a failed take's error text wraps, not clips
        self.canvas.setStyleSheet("background:#111; color:#888;")
        # Right-click the video -> show the take's original generation settings.
        self.canvas.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.canvas.customContextMenuRequested.connect(self._on_canvas_menu)

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

        # A star toggle sits by the frame timer so the keep/cull decision - made right here in
        # the viewer - no longer requires going back to the grid's tiny badge (card UX4). It
        # write-throughs like the grid badge (no project dirty) and is always live (the star is
        # a take property, available before frames decode). "S" toggles it from the keyboard.
        self.star_btn = QPushButton()
        self.star_btn.setCheckable(True)
        self.star_btn.setToolTip("Star this take (keep it) — shortcut: S")
        self.star_btn.toggled.connect(self._on_star_toggled)
        self._refresh_star_btn()
        # Scope the "S" shortcut to THIS tab's focus (WidgetWithChildren), not the whole window
        # (a QPushButton.setShortcut / WindowShortcut default would fire for the whole MainWindow):
        # otherwise a bare "s" typed anywhere in the window - e.g. a prompt box in a shot tab -
        # would trigger it, and two open player tabs would both claim "S" (ambiguous). A QAction on
        # this widget with WidgetWithChildrenShortcut context fires only when focus is inside the tab.
        star_sc = QAction(self)
        star_sc.setShortcut(QKeySequence("S"))
        star_sc.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        star_sc.triggered.connect(self.star_btn.toggle)
        self.addAction(star_sc)

        # The settings toggle sits to the right of the frame timer; it's always live
        # (the snapshot is available even before frames finish decoding).
        self.settings_btn = QPushButton("⚙ Settings")
        self.settings_btn.setCheckable(True)
        self.settings_btn.setToolTip("Show the generation settings that produced this take")
        self.settings_btn.toggled.connect(self._on_settings_toggled)

        for b in (self.prev_btn, self.play_btn, self.stop_btn, self.next_btn):
            b.setEnabled(False)

        controls = QHBoxLayout()
        controls.addWidget(self.prev_btn)
        controls.addWidget(self.play_btn)
        controls.addWidget(self.stop_btn)
        controls.addWidget(self.next_btn)
        controls.addWidget(self.slider, 1)
        controls.addWidget(self.frame_label)
        controls.addWidget(self.star_btn)
        controls.addWidget(self.settings_btn)

        center = QWidget()
        center_lay = QVBoxLayout(center)
        center_lay.setContentsMargins(0, 0, 0, 0)
        center_lay.addWidget(self.canvas, 1)
        center_lay.addLayout(controls)

        # An inner QMainWindow hosts the video as its central widget and the settings panel
        # as a dockable (floatable/redockable) QDockWidget to its right - a true "docked
        # window" per the card, scoped to this take tab. Hidden until the user asks for it.
        self._inner = QMainWindow()
        self._inner.setCentralWidget(center)

        self.settings_panel = QTextEdit()
        self.settings_panel.setReadOnly(True)
        self.settings_dock = QDockWidget("Generation settings", self._inner)
        self.settings_dock.setObjectName("generation_settings_dock")
        self.settings_dock.setWidget(self.settings_panel)
        self.settings_dock.setAllowedAreas(
            Qt.DockWidgetArea.LeftDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea)
        self._inner.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.settings_dock)
        self.settings_dock.hide()
        self.settings_dock.visibilityChanged.connect(self._on_dock_visibility)
        self._settings_loaded = False

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self._inner)

    # ---- settings panel -------------------------------------------------
    def _ensure_settings_text(self) -> None:
        if self._settings_loaded:
            return
        take = self.project.get_take(self.take_id)
        self.settings_panel.setPlainText(format_generation_settings(take))
        self._settings_loaded = True

    def show_settings(self) -> None:
        """Reveal the docked generation-settings panel (used by the button and the menu)."""
        self._ensure_settings_text()
        self.settings_dock.show()
        self.settings_dock.raise_()

    def _on_settings_toggled(self, checked: bool) -> None:
        if checked:
            self.show_settings()
        else:
            self.settings_dock.hide()

    # ---- star toggle ----------------------------------------------------
    def _current_starred(self) -> bool:
        take = self.project.get_take(self.take_id)
        return bool(getattr(take, "starred", False)) if take else False

    def _refresh_star_btn(self) -> None:
        """Sync the star button's checked state + label to the take's current star, without
        firing _on_star_toggled (so it's safe to call after a write-through)."""
        starred = self._current_starred()
        self.star_btn.blockSignals(True)
        self.star_btn.setChecked(starred)
        self.star_btn.blockSignals(False)
        self.star_btn.setText(star_button_text(starred))

    def _on_star_toggled(self, checked: bool) -> None:
        """Write the star through (same instant-persist path as the grid badge - no project
        dirty, rule: shot/take stars write through) and tell MainWindow so it refreshes the
        matching grid tile. A missing take (deleted underneath) just re-syncs the button."""
        take = self.project.get_take(self.take_id)
        if take is None:
            self._refresh_star_btn()
            return
        self.project.set_starred(self.take_id, checked)
        self.star_btn.setText(star_button_text(checked))
        self.star_changed.emit(self.take_id)

    def _on_dock_visibility(self, visible: bool) -> None:
        # Keep the toggle button in sync when the dock is closed via its own [x].
        if self.settings_btn.isChecked() != visible:
            self.settings_btn.blockSignals(True)
            self.settings_btn.setChecked(visible)
            self.settings_btn.blockSignals(False)
        if visible:
            self._ensure_settings_text()

    def _build_context_menu(self) -> QMenu:
        """The video's right-click menu (split out so it's testable without exec())."""
        menu = QMenu(self)
        has_source = self._gif_source() is not None
        save_gif = menu.addAction("Save as GIF…")
        save_gif.triggered.connect(self._save_gif)
        save_gif.setEnabled(has_source)
        copy_gif = menu.addAction("Copy GIF to clipboard")
        copy_gif.triggered.connect(self._copy_gif)
        copy_gif.setEnabled(has_source)
        menu.addSeparator()
        menu.addAction("Show generation settings").triggered.connect(self.show_settings)
        return menu

    def _on_canvas_menu(self, pos) -> None:
        self._build_context_menu().exec(self.canvas.mapToGlobal(pos))

    # ---- GIF export -----------------------------------------------------
    def _gif_source(self) -> Optional[str]:
        """The take's playable file, or None — gates the GIF menu entries."""
        take = self.project.get_take(self.take_id)
        return take_source(take) if take else None

    def _default_gif_name(self) -> str:
        """A friendly default filename: <shot-name>_<short-take-id>.gif."""
        take = self.project.get_take(self.take_id)
        shot = self.project.get_shot(take.shot_id) if take else None
        base = (shot.name if shot else "") or "take"
        safe = "".join(c if (c.isalnum() or c in "-_") else "_" for c in base).strip("_") or "take"
        return f"{safe}_{self.take_id[:8]}.gif"

    def _save_gif(self) -> None:
        source = self._gif_source()
        if not source or self._gif_busy:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Save take as GIF", self._default_gif_name(), "Animated GIF (*.gif)")
        if not path:
            return
        if not path.lower().endswith(".gif"):
            path += ".gif"
        self.canvas.setToolTip("Encoding GIF…")
        self._start_gif_export(source, path, self._on_gif_saved)

    def _copy_gif(self) -> None:
        source = self._gif_source()
        if not source or self._gif_busy:
            return
        # The temp file must outlive the copy (paste happens later), so it's written under a
        # stable per-take path and reused on repeat copies rather than auto-deleted.
        tmp_dir = Path(tempfile.gettempdir()) / "animgen_gif_clip"
        tmp = tmp_dir / f"{self.take_id}.gif"
        self.canvas.setToolTip("Encoding GIF…")
        self._start_gif_export(source, str(tmp), self._on_gif_copied)

    def _start_gif_export(self, source: str, out_path: str,
                          on_done: Callable[[str], None]) -> None:
        self._gif_busy = True
        self._gif_worker = _GifExporter(source, out_path)   # kept on self so it isn't GC'd
        self._gif_worker.done.connect(on_done)
        self._gif_worker.failed.connect(self._on_gif_failed)
        self._gif_worker.start()

    def _end_gif_export(self) -> None:
        self._gif_busy = False
        self.canvas.setToolTip("")

    def _on_gif_saved(self, path: str) -> None:
        self._end_gif_export()
        QMessageBox.information(self, "GIF saved", f"Saved animated GIF to:\n{path}")

    def _on_gif_copied(self, path: str) -> None:
        self._end_gif_export()
        self._set_clipboard_gif(path)
        QMessageBox.information(
            self, "GIF copied",
            "Copied the animated GIF to the clipboard.\n"
            "Paste it into a chat, email, or file manager.")

    def _on_gif_failed(self, msg: str) -> None:
        self._end_gif_export()
        QMessageBox.warning(self, "GIF export failed", f"Could not create the GIF:\n{msg}")

    @staticmethod
    def _set_clipboard_gif(path: str) -> None:
        """Put the on-disk GIF on the clipboard as a file reference (CF_HDROP on Windows), so
        pasting into a chat / email / file manager carries the *animated* file."""
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(path))])
        QApplication.clipboard().setMimeData(mime)

    # ---- load -----------------------------------------------------------
    def _load(self) -> None:
        take = self.project.get_take(self.take_id)
        source = take_source(take) if take else None
        if not source:
            self.canvas.setText(failure_message(take) or "This take has no playable video yet.")
            if take is not None and take.status == STATUS_FAILED:
                # Make a failure read as a failure, not a still-decoding placeholder.
                self.canvas.setStyleSheet("background:#1a1111; color:#e89090; padding:24px;")
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
