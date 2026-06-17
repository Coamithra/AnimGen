"""Animation Generator - entry point.

Run from the project root with the animgen venv, e.g.:
    animgen/.venv/Scripts/python.exe animgen/app.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# animgen/ is the import root for `paths`, `library`, `store`, `ui`, `backends`...
sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtGui import QIcon  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import applog  # noqa: E402
import paths  # noqa: E402
from store.project import Project  # noqa: E402
from ui.main_window import MainWindow  # noqa: E402


def _app_icon() -> QIcon:
    """The window/taskbar icon. Prefer the multi-size .ico on Windows; fall back to
    the vector source (which Qt rasterizes on demand) if the .ico isn't built yet."""
    if paths.APP_ICON_ICO.exists():
        return QIcon(str(paths.APP_ICON_ICO))
    return QIcon(str(paths.APP_ICON_SVG))


def _set_windows_app_id() -> None:
    """Detach our taskbar identity from python.exe so Windows shows our own icon
    (and groups our windows under it). Must run before any window is created."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Coamithra.AnimGen")
    except Exception:  # noqa: BLE001 - cosmetic only; never block startup
        pass


def _resolve_project() -> Project:
    """Reopen the last project, else the seeded starter, else a fresh untitled one."""
    if paths.APP_STATE.exists():
        try:
            last = json.loads(paths.APP_STATE.read_text(encoding="utf-8")).get("last_project")
            if last and Path(last).exists():
                return Project.load(last)
        except Exception:  # noqa: BLE001 - fall through to the next option
            pass
    if paths.DEFAULT_PROJECT.exists():
        try:
            return Project.load(paths.DEFAULT_PROJECT)
        except Exception:  # noqa: BLE001
            pass
    return Project.new()


def main() -> int:
    paths.ensure_dirs()
    log = applog.setup()
    _set_windows_app_id()
    app = QApplication(sys.argv)
    app.setApplicationName("AnimGen")
    app.setApplicationDisplayName("Animation Generator")
    app.setWindowIcon(_app_icon())
    applog.install_qt_message_handler()
    applog.install_session_logging(app)
    app.aboutToQuit.connect(lambda: log.info("aboutToQuit: event loop ending"))
    project = _resolve_project()
    log.info("opening project: %s", project.path or "<untitled>")
    win = MainWindow(project)
    applog.start_watchdog()
    applog.start_heartbeat(win, context_fn=getattr(win, "_monitor_context", None))
    win.show()
    log.info("entering event loop")
    code = app.exec()
    log.info("=== SHUTDOWN  event loop exited  code=%s ===", code)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
