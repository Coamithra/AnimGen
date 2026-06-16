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
import paths  # noqa: E402
from backends import comfy_client, replicate_client  # noqa: E402
from paths import WORKFLOWS_DIR  # noqa: E402
from store.project import Project  # noqa: E402
from store.models import STATUS_DONE, STATUS_FAILED, STATUS_PENDING  # noqa: E402

paths.SCRATCH_DIR = Path(tempfile.mkdtemp())  # keep untitled-project scratch out of data/


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


def test_capability_sync() -> None:
    # derive_capabilities reads field presence off a live input schema (card #22). It syncs
    # only the two NEW flags - supports_end_frame stays hand-authored, so it's NOT derived.
    caps = replicate_client.derive_capabilities(
        {"prompt": {}, "negative_prompt": {}, "last_frame_image": {}})
    assert caps == {"supports_negative_prompt": True, "supports_camera_fixed": False}, caps
    caps2 = replicate_client.derive_capabilities({"image": {}, "camera_fixed": {"type": "boolean"}})
    assert caps2 == {"supports_negative_prompt": False, "supports_camera_fixed": True}, caps2

    # _apply_capabilities is pure (no IO): merges + reports only the changed flags.
    doc = {"models": [{"id": "x", "supports_negative_prompt": False, "supports_camera_fixed": True}]}
    diff = library._apply_capabilities(
        doc, "x", {"supports_negative_prompt": True, "supports_camera_fixed": True})
    assert diff == {"supports_negative_prompt": (False, True)}, diff
    assert doc["models"][0]["supports_negative_prompt"] is True
    assert library._apply_capabilities(doc, "x", {"supports_negative_prompt": True}) == {}  # no-op
    assert library._apply_capabilities(doc, "missing", {"supports_camera_fixed": True}) == {}

    # sync_model_capabilities: the lock + atomic-write wrapper. Point the loader at a temp
    # roster so the real model_library.json is untouched, then assert it only rewrites on a
    # real change and the diff round-trips through disk.
    saved_path = library.MODEL_LIBRARY_PATH
    tmp = Path(tempfile.mkdtemp()) / "roster.json"
    tmp.write_text(json.dumps({"version": 1, "models": [
        {"id": "m1", "supports_negative_prompt": False, "supports_camera_fixed": False}]}),
        encoding="utf-8")
    library.MODEL_LIBRARY_PATH = tmp
    try:
        diff = library.sync_model_capabilities(
            "m1", {"supports_negative_prompt": True, "supports_camera_fixed": False})
        assert diff == {"supports_negative_prompt": (False, True)}, diff
        assert library.get_model("m1")["supports_negative_prompt"] is True  # persisted to disk
        before = tmp.read_bytes()
        # no-op: identical caps must NOT rewrite the file (empty diff, bytes unchanged)
        assert library.sync_model_capabilities(
            "m1", {"supports_negative_prompt": True, "supports_camera_fixed": False}) == {}
        assert tmp.read_bytes() == before, "no-op sync rewrote the file"
    finally:
        library.MODEL_LIBRARY_PATH = saved_path

    # Every Replicate roster entry carries well-formed boolean capability flags (the two
    # synced flags + the authored supports_end_frame).
    for m in library.models():
        if m["backend"] == "replicate":
            for k in ("supports_negative_prompt", "supports_camera_fixed", "supports_end_frame"):
                assert isinstance(m.get(k), bool), (m["id"], k)
    print("capability sync OK: derive + apply + sync write/no-op + roster flags well-formed")


def test_roster_integrity() -> None:
    # Every roster entry is well-formed for its backend (offline: no schema fetch).
    for m in library.models():
        mid = m.get("id")
        assert mid and m.get("display_name") and m.get("backend"), m
        assert library.aspect_ratios(mid), mid                 # never empty (falls back to 1:1)
        if m["backend"] == "replicate":
            assert m.get("replicate_model_id"), mid
        elif m["backend"] == "comfyui":
            assert m.get("workflow_template") and m.get("comfy_nodes"), mid

    # The two Seedance 1 entries added for card #14 (Replicate IDs verified live 2026-06-16).
    expected = {
        "seedance-1.0-pro":  ("bytedance/seedance-1-pro",  [2, 12], 0.27),
        "seedance-1.0-lite": ("bytedance/seedance-1-lite", [4, 12], 0.195),
    }
    for mid, (rmid, dur_range, cost_720p_5s) in expected.items():
        m = library.get_model(mid)
        assert m, f"{mid} missing from roster"
        assert m["replicate_model_id"] == rmid, m["replicate_model_id"]
        assert m["supports_end_frame"] is True and m["duration_range"] == dur_range, mid
        assert m["resolution_options"] == ["480p", "720p", "1080p"], mid
        assert set(m["cost_per_second_usd"]) == {"480p", "720p", "1080p"}, mid
        dp = m["default_params"]
        assert dp["camera_fixed"] is True, mid
        # aspect_ratio is ignored when an image is supplied, so it must NOT be a default
        # param (else shot_tab would send it) - this is the contract that drops the lock.
        assert "aspect_ratio" not in dp, mid
        assert abs(library.estimate_cost(mid, {"resolution": "720p", "duration": 5}) - cost_720p_5s) < 1e-9, mid
    print("roster integrity OK: backends well-formed + Seedance 1 pro/lite contract")


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

    # no end frame -> open-ended: the end-image node (10) is left with no consumers,
    # so the Wan first-last node runs like I2V instead of reusing the baked end frame.
    wf3 = comfy_client.prepare_workflow(
        template, start_img=str(a), end_img=None, prompt="P", negative="N",
        seed=1, node_roles=roles)
    assert wf3["9"]["inputs"]["image"] == "a.png"            # start still applied
    assert not any(isinstance(v, list) and v and str(v[0]) == "10"
                   for n in wf3.values() for v in n.get("inputs", {}).values()), \
        "end-image node should have no consumers when no end frame is given"
    print("comfy prepare_workflow OK: node-role map + heuristic fallback + --set + open-ended sever")


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


def test_total_price() -> None:
    from ui.cost_confirm import total_price_text

    # Hosted estimates sum; local $0 adds nothing; None tallies as unknown.
    assert total_price_text([0.72, 0.0, 1.28]) == "Full set: $2.00"
    assert total_price_text([0.72, None, 0.0]) == "Full set: $0.72  (+1 unknown)"
    assert total_price_text([]) == "Full set: $0.00"
    assert total_price_text([None, None]) == "Full set: $0.00  (+2 unknown)"
    print("cost_confirm total_price_text OK: sum, free, unknown tally, empty")


def test_cost_summary() -> None:
    from ui.cost_confirm import build_summary

    items = [
        {"name": "kick", "model_display": "Seedance 2.0 (Std)", "est_cost": 0.72,
         "params": {"duration": 4, "seed": 7, "aspect_ratio": "1:1"}},
        {"name": "tween", "model_display": "Wan 2.2 14B (local)", "est_cost": 0.0,
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
    project = Project.new()
    shot = project.add_shot("kick", model_id="seedance-2.0-std")
    jm = JobManager(project)
    done, failed, progressed = [], [], []
    jm.finished.connect(done.append)
    jm.failed.connect(lambda tid, err: failed.append((tid, err)))
    jm.progress.connect(lambda tid, line: progressed.append(line))

    ok = project.add_take(shot.id, status=STATUS_PENDING)

    def good_runner(progress):
        progress("uploading")
        progress("processing")
        return {"video_path": "x.mp4", "fps": 16.0, "frame_count": 33}

    jm.enqueue(ok.id, "replicate", good_runner)

    bad = project.add_take(shot.id, status=STATUS_PENDING)

    def bad_runner(progress):
        progress("starting")
        raise RuntimeError("boom")

    jm.enqueue(bad.id, "replicate", bad_runner)

    assert jm.wait_for_done(20000), "jobs did not finish"
    app.processEvents()

    got_ok = project.get_take(ok.id)
    assert got_ok.status == STATUS_DONE and got_ok.video_path == "x.mp4" and got_ok.fps == 16.0
    got_bad = project.get_take(bad.id)
    assert got_bad.status == STATUS_FAILED and "boom" in (got_bad.error or "")
    assert ok.id in done and any(tid == bad.id for tid, _ in failed)
    assert "uploading" in progressed and "starting" in progressed
    print("JobManager OK: pending->generating->done + failure path + signals")


def test_cancel_pending() -> None:
    import threading
    import time

    from PySide6.QtWidgets import QApplication

    from backends.jobs import JobManager
    from store.models import STATUS_CANCELLED, STATUS_DONE, STATUS_GENERATING

    app = QApplication.instance() or QApplication([])
    project = Project.new()
    shot = project.add_shot("kick", model_id="local-flf-wan14b")
    jm = JobManager(project)

    release = threading.Event()
    active = project.add_take(shot.id, status=STATUS_PENDING)
    q1 = project.add_take(shot.id, status=STATUS_PENDING)
    q2 = project.add_take(shot.id, status=STATUS_PENDING)

    def blocker(progress):  # occupies the single local worker until released
        release.wait(timeout=10)
        return {"video_path": "x.mp4"}

    def quick(progress):
        return {"video_path": "y.mp4"}

    jm.enqueue(active.id, "comfyui", blocker)   # local pool is max 1 -> this one runs,
    jm.enqueue(q1.id, "comfyui", quick)         # these two wait in the queue
    jm.enqueue(q2.id, "comfyui", quick)

    for _ in range(100):  # wait until the blocker is actually generating
        if project.get_take(active.id).status == STATUS_GENERATING:
            break
        time.sleep(0.02)
    assert jm.pending_count() == 2, jm.pending_count()

    n = jm.cancel_pending()
    assert n == 2, n
    release.set()
    assert jm.wait_for_done(10000), "jobs did not finish"
    app.processEvents()

    assert project.get_take(q1.id).status == STATUS_CANCELLED
    assert project.get_take(q2.id).status == STATUS_CANCELLED
    assert project.get_take(active.id).status == STATUS_DONE  # the running one was untouched
    print("cancel_pending OK: queued cancelled, in-progress job left running")


if __name__ == "__main__":
    test_build_input()
    test_capability_sync()
    test_roster_integrity()
    test_comfy_prepare()
    test_dynamic_vram_gate()
    test_comfy_launch_helpers()
    test_comfy_stop_helpers()
    test_total_price()
    test_cost_summary()
    test_cancel_pending()
    test_job_manager()
    print("PHASE 2 SMOKE: PASS")
