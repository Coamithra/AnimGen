"""Phase 6 smoke test (offscreen, no spend).

Seeds the real shipped-move manifest into a temp db (idempotent), and builds the
model library window. Does NOT touch the live animgen.db or any project assets.

    QT_QPA_PLATFORM=offscreen PYTHONIOENCODING=utf-8 \
        animgen/.venv/Scripts/python.exe animgen/scripts/smoke_phase6.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # animgen/

import paths  # noqa: E402
from store.db import Store  # noqa: E402


def test_seed() -> None:
    from scripts.seed_configs import seed

    st = Store(Path(tempfile.mkdtemp()) / "seed.db")
    out_dir = paths.FIGHTER_OUT
    previews = out_dir / "retime_previews"
    added = seed(st, paths.GAME_SPRITES_MANIFEST, out_dir, previews)
    assert added >= 25, f"expected ~31 moves, seeded {added}"
    configs = st.list_configs()
    assert len(configs) == added
    # every seeded config has exactly one starred 'done' result
    for c in configs:
        rs = st.list_results(c.id)
        assert len(rs) == 1 and rs[0].starred and rs[0].status == "done"
        assert rs[0].settings_snapshot.get("move") == c.name
    # models were derived across backends
    models = {c.model_id for c in configs}
    assert "seedance-2.0-std" in models
    # idempotent: a second seed adds nothing
    again = seed(st, paths.GAME_SPRITES_MANIFEST, out_dir, previews)
    assert again == 0 and len(st.list_configs()) == added
    # at least some takes/previews resolved to real files
    with_video = sum(1 for c in configs for r in st.list_results(c.id) if r.video_path)
    with_thumb = sum(1 for c in configs for r in st.list_results(c.id) if r.thumbnail)
    st.close()
    print(f"seed OK: {added} configs, all starred/done; {with_video} with video, "
          f"{with_thumb} with preview thumb; idempotent")


def test_library_window() -> None:
    from PySide6.QtWidgets import QApplication, QTableWidget

    import library
    from ui.model_library_window import ModelLibraryWindow

    app = QApplication.instance() or QApplication([])  # noqa: F841
    win = ModelLibraryWindow()
    table = win.findChild(QTableWidget)
    assert table is not None and table.rowCount() == len(library.models())
    print(f"ModelLibraryWindow OK: {table.rowCount()} model rows")


if __name__ == "__main__":
    test_seed()
    test_library_window()
    print("PHASE 6 SMOKE: PASS")
