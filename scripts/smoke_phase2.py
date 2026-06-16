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


def test_dynamic_vram_gate() -> None:
    # Mirrors ComfyUI's enables_dynamic_vram(): ON by default, off only with a disabler.
    base = ["main.py", "--listen", "127.0.0.1", "--port", "8188"]
    assert comfy_client.dynamic_vram_enabled(base) is True
    assert comfy_client.dynamic_vram_enabled(base + ["--disable-dynamic-vram"]) is False
    for disabler in ("--highvram", "--gpu-only", "--novram", "--cpu"):
        assert comfy_client.dynamic_vram_enabled(base + [disabler]) is False, disabler
    print("comfy dynamic-VRAM gate OK: default-on, off on each disabling flag")


def test_comfy_launch_helpers() -> None:
    cmd = comfy_client.build_launch_command()
    assert cmd[1].endswith("main.py")
    assert "--disable-dynamic-vram" in cmd and "--port" in cmd
    # overriding a default flag drops its value too (no orphaned 8188), keeps the flag
    over = comfy_client.build_launch_command(["--port", "8189"])
    assert "8188" not in over and over[-2:] == ["--port", "8189"]
    assert "--disable-dynamic-vram" in over
    # status probe is non-raising and well-shaped whether or not a server is up
    st = comfy_client.server_status(timeout=1)
    assert set(st) == {"running", "version", "dynamic_vram", "argv"}
    assert isinstance(st["running"], bool)
    # monitor snapshot + models list are non-raising too (the monitor window relies on it)
    snap = comfy_client.monitor_snapshot(timeout=1)
    assert isinstance(snap, dict) and isinstance(snap["running"], bool)
    if not snap["running"]:
        assert snap == {"running": False}
    assert isinstance(comfy_client.list_models(timeout=1), dict)
    print("comfy launch helpers OK: command flags + non-raising status/monitor probes")


def test_comfy_stop_helpers() -> None:
    # pid-by-port lookup is read-only and non-raising (int when a server is up, else None)
    pid = comfy_client._pid_on_port(comfy_client.COMFY_PORT)
    assert pid is None or isinstance(pid, int)
    # stop_work surfaces a ComfyError when ComfyUI is unreachable. Point at a dead port so
    # this never touches a real server (which it would interrupt).
    saved = comfy_client.COMFY_URL
    comfy_client.COMFY_URL = "http://127.0.0.1:1"
    try:
        raised = False
        try:
            comfy_client.stop_work(timeout=1)
        except comfy_client.ComfyError:
            raised = True
        assert raised, "stop_work should raise ComfyError when ComfyUI is unreachable"
    finally:
        comfy_client.COMFY_URL = saved
    print("comfy stop helpers OK: pid-by-port probe + stop_work error path")


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


def test_cancel_pending() -> None:
    import threading
    import time

    from PySide6.QtWidgets import QApplication

    from backends.jobs import JobManager
    from store.models import STATUS_CANCELLED, STATUS_DONE, STATUS_GENERATING

    app = QApplication.instance() or QApplication([])
    st = Store(Path(tempfile.mkdtemp()) / "cancel.db")
    cfg = st.add_config("kick", model_id="local-flf-wan14b")
    jm = JobManager(st)

    release = threading.Event()
    active = st.add_result(cfg.id, status=STATUS_PENDING)
    q1 = st.add_result(cfg.id, status=STATUS_PENDING)
    q2 = st.add_result(cfg.id, status=STATUS_PENDING)

    def blocker(progress):  # occupies the single local worker until released
        release.wait(timeout=10)
        return {"video_path": "x.mp4"}

    def quick(progress):
        return {"video_path": "y.mp4"}

    jm.enqueue(active.id, "comfyui", blocker)   # local pool is max 1 -> this one runs,
    jm.enqueue(q1.id, "comfyui", quick)         # these two wait in the queue
    jm.enqueue(q2.id, "comfyui", quick)

    for _ in range(100):  # wait until the blocker is actually generating
        if st.get_result(active.id).status == STATUS_GENERATING:
            break
        time.sleep(0.02)
    assert jm.pending_count() == 2, jm.pending_count()

    n = jm.cancel_pending()
    assert n == 2, n
    release.set()
    assert jm.wait_for_done(10000), "jobs did not finish"
    app.processEvents()

    assert st.get_result(q1.id).status == STATUS_CANCELLED
    assert st.get_result(q2.id).status == STATUS_CANCELLED
    assert st.get_result(active.id).status == STATUS_DONE  # the running one was untouched
    st.close()
    print("cancel_pending OK: queued cancelled, in-progress job left running")


if __name__ == "__main__":
    test_build_input()
    test_comfy_prepare()
    test_dynamic_vram_gate()
    test_comfy_launch_helpers()
    test_comfy_stop_helpers()
    test_cost_summary()
    test_cancel_pending()
    test_job_manager()
    print("PHASE 2 SMOKE: PASS")
