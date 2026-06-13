"""Phase 2 smoke test (no spend, no ComfyUI required).

Covers: hosted field mapping (build_input), local workflow prep (node-role + heuristic
+ --set), cost-summary math, and the JobManager driving a fake runner through
pending -> generating -> done and the failure path.

    QT_QPA_PLATFORM=offscreen PYTHONIOENCODING=utf-8 \
        animgen/.venv/Scripts/python.exe animgen/scripts/smoke_phase2.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # animgen/

import library  # noqa: E402
from backends import comfy_client, replicate_client  # noqa: E402
from paths import WORKFLOWS_DIR  # noqa: E402
from store.db import Store  # noqa: E402
from store.models import STATUS_DONE, STATUS_FAILED, STATUS_PENDING  # noqa: E402


def test_build_input() -> None:
    props = {
        "image": {"type": "string"}, "last_frame_image": {"type": "string"},
        "prompt": {"type": "string"}, "negative_prompt": {"type": "string"},
        "duration": {"type": "integer"}, "resolution": {"type": "string"},
        "seed": {"type": "integer"}, "aspect_ratio": {"type": "string"},
        "camera_fixed": {"type": "boolean"}, "generate_audio": {"type": "boolean"},
    }
    inp = replicate_client.build_input(
        props, start_url="S", end_url="E", prompt="P", negative="N", duration=4,
        resolution="720p", seed=7, extra={"aspect_ratio": "1:1", "camera_fixed": True})
    assert inp["image"] == "S" and inp["last_frame_image"] == "E"
    assert inp["prompt"] == "P" and inp["negative_prompt"] == "N"
    assert inp["duration"] == 4 and inp["resolution"] == "720p" and inp["seed"] == 7
    assert inp["aspect_ratio"] == "1:1" and inp["camera_fixed"] is True
    assert inp["generate_audio"] is False
    print("replicate build_input OK: canonical->schema mapping, audio off, extra coerced")


def test_comfy_prepare() -> None:
    from PIL import Image

    tmp = Path(tempfile.mkdtemp())
    comfy_client.COMFY_INPUT_DIR = tmp / "input"   # don't touch the real ComfyUI dir
    a, b = tmp / "a.png", tmp / "b.png"
    Image.new("RGB", (8, 8), (255, 0, 255)).save(a)
    Image.new("RGB", (8, 8), (0, 0, 0)).save(b)

    template = json.loads((WORKFLOWS_DIR / "FLF_stand_to_crouch.json").read_text(encoding="utf-8"))
    roles = library.get_model("local-flf-wan14b")["comfy_nodes"]

    wf = comfy_client.prepare_workflow(
        template, start_img=str(a), end_img=str(b), prompt="POS", negative="NEG",
        seed=42, node_roles=roles, sets={"12.steps": "30"})
    assert wf["9"]["inputs"]["image"] == "a.png"
    assert wf["10"]["inputs"]["image"] == "b.png"
    assert wf["7"]["inputs"]["text"] == "POS"
    assert wf["8"]["inputs"]["text"] == "NEG"
    assert wf["12"]["inputs"]["noise_seed"] == 42 and wf["13"]["inputs"]["noise_seed"] == 42
    assert wf["12"]["inputs"]["steps"] == 30  # --set int-coerced

    # heuristic fallback (no roles): same result via ascending node-id ordering
    wf2 = comfy_client.prepare_workflow(
        template, start_img=str(a), end_img=str(b), prompt="P2", negative="N2", seed=9)
    assert wf2["9"]["inputs"]["image"] == "a.png" and wf2["10"]["inputs"]["image"] == "b.png"
    assert wf2["7"]["inputs"]["text"] == "P2" and wf2["8"]["inputs"]["text"] == "N2"
    assert wf2["12"]["inputs"]["noise_seed"] == 9
    print("comfy prepare_workflow OK: node-role map + heuristic fallback + --set")


def test_cost_summary() -> None:
    from ui.cost_confirm import build_summary

    items = [
        {"name": "kick", "model_display": "Seedance 2.0 (std · 720p)", "est_cost": 0.72,
         "params": {"duration": 4, "seed": 7, "aspect_ratio": "1:1"}},
        {"name": "tween", "model_display": "Local FLF tween (Wan 14B)", "est_cost": 0.0,
         "params": {"seed": 7}},
    ]
    body, total, has_spend = build_summary(items)
    assert abs(total - 0.72) < 1e-9 and has_spend is True
    assert "Seedance" in body and "$0.72" in body and "free" in body
    body2, total2, has_spend2 = build_summary([items[1]])
    assert total2 == 0.0 and has_spend2 is False
    print("cost_confirm build_summary OK: totals, spend flag, free-only")


def test_job_manager() -> None:
    from PySide6.QtWidgets import QApplication

    from backends.jobs import JobManager

    app = QApplication.instance() or QApplication([])
    st = Store(Path(tempfile.mkdtemp()) / "j.db")
    cfg = st.add_config("kick", model_id="seedance-2.0-std")
    jm = JobManager(st)
    done, failed, progressed = [], [], []
    jm.finished.connect(done.append)
    jm.failed.connect(lambda rid, err: failed.append((rid, err)))
    jm.progress.connect(lambda rid, line: progressed.append(line))

    ok = st.add_result(cfg.id, status=STATUS_PENDING)

    def good_runner(progress):
        progress("uploading")
        progress("processing")
        return {"video_path": "x.mp4", "fps": 16.0, "frame_count": 33}

    jm.enqueue(ok.id, "replicate", good_runner)

    bad = st.add_result(cfg.id, status=STATUS_PENDING)

    def bad_runner(progress):
        progress("starting")
        raise RuntimeError("boom")

    jm.enqueue(bad.id, "replicate", bad_runner)

    assert jm.wait_for_done(20000), "jobs did not finish"
    app.processEvents()

    got_ok = st.get_result(ok.id)
    assert got_ok.status == STATUS_DONE and got_ok.video_path == "x.mp4" and got_ok.fps == 16.0
    got_bad = st.get_result(bad.id)
    assert got_bad.status == STATUS_FAILED and "boom" in (got_bad.error or "")
    assert ok.id in done and any(rid == bad.id for rid, _ in failed)
    assert "uploading" in progressed and "starting" in progressed
    st.close()
    print("JobManager OK: pending->generating->done + failure path + signals")


if __name__ == "__main__":
    test_build_input()
    test_comfy_prepare()
    test_cost_summary()
    test_job_manager()
    print("PHASE 2 SMOKE: PASS")
