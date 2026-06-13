"""Phase 1 smoke test: library load, db round-trip, offscreen GUI build.

Run headless with the animgen venv:
    QT_QPA_PLATFORM=offscreen PYTHONIOENCODING=utf-8 \
        animgen/.venv/Scripts/python.exe animgen/scripts/smoke_phase1.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # animgen/

import library  # noqa: E402
import paths  # noqa: E402
from store.db import Store  # noqa: E402
from store.models import STATUS_DONE  # noqa: E402


def test_library() -> None:
    lib = library.load_library()
    assert lib["models"], "no models in library"
    seed = library.get_model("seedance-2.0-std")
    assert seed and seed["replicate_model_id"] == "bytedance/seedance-2.0"
    cost = library.estimate_cost("seedance-2.0-std", {"duration": 4})
    assert cost is not None and abs(cost - 0.72) < 1e-6, cost
    assert library.estimate_cost("local-flf-wan14b", {}) == 0.0
    assert library.default_negative_prompt().startswith("camera pan")
    print(f"library OK: {len(lib['models'])} models; seedance 4s est ${cost:.2f}")


def test_db() -> None:
    tmp = Path(tempfile.mkdtemp()) / "t.db"
    st = Store(tmp)
    cfg = st.add_config(
        "kick_heavy", model_id="seedance-2.0-std", start_frame="assets/a.png",
        settings={"seed": 7, "duration": 4}, crop={"x": 1, "y": 2, "w": 3, "h": 4},
    )
    got = st.get_config(cfg.id)
    assert got.name == "kick_heavy" and got.settings["seed"] == 7 and got.crop["w"] == 3
    st.update_config(cfg.id, prompt="fierce kick", settings={"seed": 9, "duration": 5})
    got = st.get_config(cfg.id)
    assert got.prompt == "fierce kick" and got.settings["seed"] == 9

    res = st.add_result(
        cfg.id, status=STATUS_DONE, seed=7,
        settings_snapshot={"model_id": "seedance-2.0-std", "seed": 7, "prompt": "fierce kick"},
    )
    assert st.get_result(res.id).settings_snapshot["seed"] == 7
    st.set_starred(res.id, True)
    assert st.list_results(cfg.id, starred_only=True)[0].id == res.id
    st.soft_delete_result(res.id)
    assert st.list_results(cfg.id) == []
    assert len(st.list_results(cfg.id, include_deleted=True)) == 1
    st.restore_result(res.id)
    assert len(st.list_results(cfg.id)) == 1
    assert st.used_model_ids() == ["seedance-2.0-std"]

    job = st.add_job(res.id, backend="replicate", state="queued")
    st.update_job(job.id, state="running", ext_id="pred_abc")
    assert st.get_job(job.id).ext_id == "pred_abc"
    st.close()
    print("db OK: config+result+job round-trip, snapshot, star/delete/restore")


def test_gui_build() -> None:
    from PySide6.QtWidgets import QApplication

    from ui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    paths.ensure_dirs()
    st = Store(paths.DB_PATH)
    win = MainWindow(st)
    win.show()
    app.processEvents()
    assert win.windowTitle() == "Animation Generator"
    st.close()
    print("GUI OK: MainWindow built + shown offscreen")


if __name__ == "__main__":
    test_library()
    test_db()
    test_gui_build()
    print("PHASE 1 SMOKE: PASS")
