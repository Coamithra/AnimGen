"""Phase 3 smoke test (offscreen, no spend).

Covers framing math (normalize_keypose), the placement canvas (aspect + sprite + drag
placement round-trip), per-model aspect dropdown + validation in the shot tab, and the
gen-time keypose render from a shot's per-keyframe placement.

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

paths.SCRATCH_DIR = Path(tempfile.mkdtemp())  # keep untitled-project scratch out of data/

from pipeline import framing  # noqa: E402
from store.project import Project  # noqa: E402


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
    # canvas_size: hosted = longest side 1254; local = ~budget, both dims /16
    assert framing.canvas_size("16:9", local=False) == (1254, 705)
    for a in ("1:1", "16:9", "9:16", "21:9"):
        w, h = framing.canvas_size(a, local=True)
        assert w % 16 == 0 and h % 16 == 0 and 380_000 < w * h < 430_000, (a, w, h)
    # display_size: hosted readout tracks the resolution tier (short side); local = render canvas
    assert framing.display_size("1:1", resolution="720p") == (720, 720)
    assert framing.display_size("1:1", resolution="480p") == (480, 480)
    assert framing.display_size("16:9", resolution="720p") == (1280, 720)
    assert framing.display_size("9:16", resolution="1080p") == (1080, 1920)
    assert framing.display_size("1:1", local=True) == framing.canvas_size("1:1", local=True)
    assert framing.display_size("16:9", resolution=None) == framing.canvas_size("16:9")
    print("framing OK: normalize_keypose + canvas_size (hosted long-side / local /16 budget)")


def test_placement_canvas() -> None:
    from PySide6.QtWidgets import QApplication

    from ui.placement_widget import PlacementCanvas, pil_to_pixmap

    app = QApplication.instance() or QApplication([])  # noqa: F841
    tmp = Path(tempfile.mkdtemp())
    src = tmp / "char.png"
    _char_image(src)
    pc = PlacementCanvas()
    pc.set_aspect(*framing.canvas_size("16:9", local=False))
    pc.set_sprite(pil_to_pixmap(framing.keyed_sprite(str(src))))
    pc.set_placement({"scale": 0.5, "cx": 0.4, "cy": 0.6})
    got = pc.get_placement()
    assert abs(got["scale"] - 0.5) < 0.03 and abs(got["cx"] - 0.4) < 0.03 \
        and abs(got["cy"] - 0.6) < 0.03, got
    print("PlacementCanvas OK: aspect + keyed sprite + placement round-trip")


def test_shot_tab() -> None:
    from PySide6.QtWidgets import QApplication

    from ui.shot_tab import ShotTab

    app = QApplication.instance() or QApplication([])  # noqa: F841
    tmp = Path(tempfile.mkdtemp())
    src = tmp / "char.png"
    _char_image(src)
    project = Project.new()
    asset = str(project.import_asset(src))

    ed = ShotTab(project)
    ed.model_combo.setCurrentIndex(ed.model_combo.findData("seedance-2.0-std"))
    aspects = [ed.aspect_combo.itemText(i) for i in range(ed.aspect_combo.count())]
    assert aspects == ["1:1", "16:9", "9:16"], aspects
    assert ed.aspect_valid()
    assert "aspect_ratio" not in ed._params()    # owned by the Aspect dropdown now
    assert ed._params()["resolution"] == "720p" and ed._params()["seed"] == 7

    ed.name.setText("kick_heavy")
    ed.prompt.setPlainText("fierce kick")
    ed._set_asset("start", asset); ed._select("start")
    ed.aspect_combo.setCurrentText("16:9")
    saved = []
    ed.saved.connect(saved.append)
    sid = ed._save()
    assert sid and saved == [sid], "save should emit saved(shot_id)"

    shot = project.list_shots()[0]
    assert shot.name == "kick_heavy" and shot.start_frame == asset
    assert shot.crop["aspect"] == "16:9" and "start" in shot.crop and "end" in shot.crop
    assert (shot.canvas_w, shot.canvas_h) == framing.canvas_size("16:9", local=False)
    assert shot.settings.get("aspect_ratio") == "16:9"   # injected for seedance
    assert not (project.assets_dir / "keyposes").exists(), "no hash keypose folders"

    # selected AR unavailable on a new model -> field invalid (Generate would refuse)
    ed.aspect_combo.setCurrentText("1:1")
    ed.model_combo.setCurrentIndex(ed.model_combo.findData("veo-3.1-fast"))  # no 1:1
    assert not ed.aspect_valid()

    # reopen round-trip
    ed2 = ShotTab(project, shot=project.get_shot(shot.id))
    assert ed2.name.text() == "kick_heavy" and ed2._assets["start"] == asset
    assert ed2.selected_aspect() == "16:9"

    # Copy Start -> End: disabled with no start, mirrors asset + placement once set
    ed3 = ShotTab(project)
    assert not ed3.copy_se_btn.isEnabled(), "copy disabled until a start frame exists"
    ed3._set_asset("start", asset)
    assert ed3.copy_se_btn.isEnabled()
    ed3._frames["start"] = {"scale": 0.42, "cx": 0.3, "cy": 0.7}
    ed3._select("end")   # end active: the live-capture path won't clobber the start frame
    ed3._copy_start_to_end()
    assert ed3._assets["end"] == asset, "end frame should mirror start asset"
    assert ed3._frames["end"] == {"scale": 0.42, "cx": 0.3, "cy": 0.7}
    assert ed3._frames["end"] is not ed3._frames["start"], "must copy, not alias"
    print("ShotTab OK: per-model aspect dropdown + validation, asset pick, save/load, copy start->end")


def test_render_keyposes() -> None:
    tmp = Path(tempfile.mkdtemp())
    src = tmp / "char.png"
    _char_image(src)
    project = Project.new()
    asset = str(project.import_asset(src))
    w, h = framing.canvas_size("16:9", local=False)

    class _Shot:  # duck-typed shot for the framing call
        start_frame = asset
        end_frame = None
        canvas_w, canvas_h = w, h
        crop = {"aspect": "16:9", "start": {"scale": 0.8, "cx": 0.5, "cy": 0.55}, "end": {}}

    out = Path(tempfile.mkdtemp())
    start_kp, end_kp = framing.render_keyposes(_Shot(), out)
    assert start_kp and Path(start_kp).exists()
    assert Image.open(start_kp).size == (w, h)
    assert Image.open(start_kp).getpixel((0, 0)) == (255, 0, 255)  # magenta bg
    assert end_kp is None
    print("render_keyposes OK: keyed sprite placed on the aspect canvas at gen time")


if __name__ == "__main__":
    test_framing()
    test_placement_canvas()
    test_shot_tab()
    test_render_keyposes()
    print("PHASE 3 SMOKE: PASS")
