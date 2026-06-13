"""Phase 4 smoke test (offscreen, no spend).

Covers bin/restore (tool-owned moved, external left in place), the results view's
filter/star/delete, config-card expansion, and the main window building cards.

    QT_QPA_PLATFORM=offscreen PYTHONIOENCODING=utf-8 \
        animgen/.venv/Scripts/python.exe animgen/scripts/smoke_phase4.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # animgen/

from PIL import Image  # noqa: E402

from store.db import Store  # noqa: E402
from store.models import STATUS_DONE  # noqa: E402


def _png(path: Path, color=(0, 200, 0)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (16, 16), color).save(path)


def test_bin_restore() -> None:
    from pipeline import results_io

    sandbox = Path(tempfile.mkdtemp())
    results_io.DATA_DIR = sandbox
    results_io.RESULTS_DIR = sandbox / "results"
    results_io.BIN_DIR = sandbox / "bin"

    st = Store(sandbox / "db.sqlite")
    cfg = st.add_config("c", model_id="seedance-2.0-std")

    # tool-owned file -> moved to bin
    owned = sandbox / "results" / cfg.id / "r.mp4"
    owned.parent.mkdir(parents=True, exist_ok=True)
    owned.write_bytes(b"video")
    r1 = st.add_result(cfg.id, status=STATUS_DONE, video_path=str(owned))
    results_io.move_to_bin(st.get_result(r1.id), st)
    g1 = st.get_result(r1.id)
    assert g1.deleted and not owned.exists()
    assert (sandbox / "bin" / r1.id / "r.mp4").exists()
    assert Path(g1.video_path).exists()
    # restore
    results_io.restore_from_bin(st.get_result(r1.id), st)
    g1 = st.get_result(r1.id)
    assert not g1.deleted and Path(g1.video_path).exists()
    assert (sandbox / "results" / cfg.id / "r.mp4").exists()

    # external file (a seeded project asset) -> NOT moved, just flagged
    ext_dir = Path(tempfile.mkdtemp())
    ext = ext_dir / "BAKE_take.mp4"
    ext.write_bytes(b"external")
    r2 = st.add_result(cfg.id, status=STATUS_DONE, video_path=str(ext))
    results_io.move_to_bin(st.get_result(r2.id), st)
    g2 = st.get_result(r2.id)
    assert g2.deleted and ext.exists() and g2.video_path == str(ext)
    st.close()
    print("results_io OK: tool-owned binned/restored, external file untouched")


def test_results_view() -> None:
    from PySide6.QtWidgets import QApplication

    from ui.results_view import ResultsView

    app = QApplication.instance() or QApplication([])  # noqa: F841
    tmp = Path(tempfile.mkdtemp())
    st = Store(tmp / "db.sqlite")
    cfg = st.add_config("c", model_id="seedance-2.0-std")
    t1, t2 = tmp / "t1.png", tmp / "t2.png"
    _png(t1); _png(t2, (200, 0, 0))
    r1 = st.add_result(cfg.id, status=STATUS_DONE, starred=True, thumbnail=str(t1))
    r2 = st.add_result(cfg.id, status=STATUS_DONE, thumbnail=str(t2))
    r3 = st.add_result(cfg.id, status=STATUS_DONE, thumbnail=str(t2), deleted=True)  # hidden

    rv = ResultsView(st, cfg.id)
    assert rv.model.rowCount() == 2, rv.model.rowCount()       # r3 hidden
    rv.filter.setCurrentText("Favorites")
    assert rv.model.rowCount() == 1                            # only r1 starred
    rv.toggle_star([r2.id])
    assert rv.model.rowCount() == 2                            # r1 + r2 now starred
    rv.filter.setCurrentText("All")
    rv.delete([r1.id])
    assert rv.model.rowCount() == 1 and st.get_result(r1.id).deleted
    _ = r3
    print("ResultsView OK: filter, star toggle, delete-to-bin, counts")


def test_card_and_window() -> None:
    from PySide6.QtWidgets import QApplication

    from ui.config_card import ConfigCard
    from ui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])  # noqa: F841
    tmp = Path(tempfile.mkdtemp())
    st = Store(tmp / "db.sqlite")
    cfg = st.add_config("kick", model_id="seedance-2.0-std",
                        settings={"seed": 7, "duration": 4, "resolution": "720p"})
    r = st.add_result(cfg.id, status=STATUS_DONE, starred=True)

    card = ConfigCard(st, cfg)
    assert "1 results" in card.counts.text() and "1★" in card.counts.text()
    card.expand_btn.setChecked(True)
    assert card.results_view is not None and card.results_view.model.rowCount() == 1
    card.expand_btn.setChecked(False)
    assert not card.body.isVisible()

    win = MainWindow(st)
    assert len(win.cards) == 1 and cfg.id in win.cards
    assert win._card_for_result(r.id) is win.cards[cfg.id]
    st.close()
    print("ConfigCard + MainWindow OK: counts, expand, card routing")


if __name__ == "__main__":
    test_bin_restore()
    test_results_view()
    test_card_and_window()
    print("PHASE 4 SMOKE: PASS")
