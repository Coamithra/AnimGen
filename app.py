"""Animation Generator - entry point.

Run from the project root with the animgen venv, e.g.:
    animgen/.venv/Scripts/python.exe animgen/app.py
"""
from __future__ import annotations

import sys
from pathlib import Path

# animgen/ is the import root for `paths`, `library`, `store`, `ui`, `backends`...
sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtWidgets import QApplication  # noqa: E402

import paths  # noqa: E402
from store.db import Store  # noqa: E402
from ui.main_window import MainWindow  # noqa: E402


def main() -> int:
    paths.ensure_dirs()
    store = Store(paths.DB_PATH)
    app = QApplication(sys.argv)
    win = MainWindow(store)
    win.show()
    code = app.exec()
    store.close()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
