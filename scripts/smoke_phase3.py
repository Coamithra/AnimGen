"""Phase 3 smoke test (offscreen, no spend).

Covers framing math (crop + scale-to-height + magenta canvas), the crop widget's
crop/framing readout, and the config editor's save/load round-trip (bakes a
normalized start keypose and persists framing metadata).

    QT_QPA_PLATFORM=offscreen PYTHONIOENCODING=utf-8 \
        animgen/.venv/Scripts/python.exe animgen/scripts/smoke_phase3.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # animgen/

from PIL import Image, ImageDraw  # noqa: E402

import paths  # noqa: E402
from pipeline import framing  # noqa: E402
from store.db import Store  # noqa: E402


def _char_image(path: Path) -> None:
    im = Image.new("RGB", (200, 200), (255, 0, 255))
    ImageDraw.Draw(im).rectangle([80, 40, 120, 160], fill=(0, 0, 0))  # h=121, cx=100
    im.save(path)


def test_framing() -> None:
    tmp = Path(tempfile.mkdtemp())
    src = tmp / "char.png"
    _char_image(src)
    meta = framing.normalize_keypose(
        src, canvas=(400, 400), char_height_frac=0.5, ground_y=380, char_x=0.5)
    assert meta["image"].size == (400, 400)
    assert meta["image"].getpixel((0, 0)) == (255, 0, 255)  # magenta bg
    x0, y0, x1, y1 = meta["char_box"]
    target = 0.5 * 400
    assert abs((y1 - y0 + 1) - target) <= max(5, 0.06 * target), (y1 - y0 + 1, target)
    assert abs(y1 - 380) <= 3, y1                          # feet at ground line
    assert abs((x0 + x1) / 2 - 200) <= 3                   # centered
    # crop path: tight crop still frames to the target fraction
    meta2 = framing.normalize_keypose(
        src, crop=(80, 40, 41, 121), canvas=(400, 400), char_height_frac=0.8, ground_y=390)
    h2 = meta2["char_box"][3] - meta2["char_box"][1] + 1
    assert abs(h2 - 0.8 * 400) <= max(6, 0.06 * 320), h2
    print("framing OK: scale-to-height, ground placement, centering, crop")


def test_crop_widget() -> None:
    from PySide6.QtWidgets import QApplication

    from ui.crop_widget import CropWidget

    app = QApplication.instance() or QApplication([])  # noqa: F841
    tmp = Path(tempfile.mkdtemp())
    src = tmp / "char.png"
    _char_image(src)
    w = CropWidget()
    w.set_source(str(src))
    assert w.get_crop() == [0, 0, 200, 200]
    f = w.get_framing()
    assert f["canvas"] == [1254, 1254] and abs(f["char_height_frac"] - 0.65) < 1e-9
    w.sx.setValue(80); w.sy.setValue(40); w.sw.setValue(41); w.sh.setValue(121)
    assert w.get_crop() == [80, 40, 41, 121]
    out = tmp / "baked.png"
    w.bake(str(out))
    assert out.exists() and Image.open(out).size == (1254, 1254)
    print("CropWidget OK: source load, crop readout, bake")


def test_config_editor() -> None:
    from PySide6.QtWidgets import QApplication

    from ui.config_editor import ConfigEditor

    app = QApplication.instance() or QApplication([])  # noqa: F841
    tmp = Path(tempfile.mkdtemp())
    src = tmp / "char.png"
    _char_image(src)
    paths.ensure_dirs()
    st = Store(Path(tempfile.mkdtemp()) / "e.db")

    ed = ConfigEditor(st)
    idx = ed.model_combo.findData("seedance-2.0-std")
    ed.model_combo.setCurrentIndex(idx)
    p = ed._params()
    assert p["aspect_ratio"] == "1:1" and p["camera_fixed"] is True
    assert p["duration"] == 4 and p["resolution"] == "720p" and p["seed"] == 7
    ed.name.setText("kick_heavy")
    ed.prompt.setPlainText("fierce kick")
    ed.start_src.setText(str(src))
    ed.crop.set_source(str(src))
    ed._save()

    cfgs = st.list_configs()
    assert len(cfgs) == 1
    cfg = cfgs[0]
    assert cfg.name == "kick_heavy" and cfg.model_id == "seedance-2.0-std"
    assert cfg.prompt == "fierce kick"
    assert cfg.start_frame and Path(cfg.start_frame).exists()
    assert cfg.crop.get("source_start") == str(src) and cfg.canvas_w == 1254

    # reopen for edit: fields load back, second save keeps a single config
    ed2 = ConfigEditor(st, config=st.get_config(cfg.id))
    assert ed2.name.text() == "kick_heavy"
    assert ed2.model_combo.currentData() == "seedance-2.0-std"
    assert ed2._params()["seed"] == 7
    ed2._save()
    assert len(st.list_configs()) == 1
    st.close()
    print("ConfigEditor OK: params, save (bake+persist), load round-trip")


if __name__ == "__main__":
    test_framing()
    test_crop_widget()
    test_config_editor()
    print("PHASE 3 SMOKE: PASS")
