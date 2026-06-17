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
from store.models import (  # noqa: E402
    STATUS_DONE, STATUS_FAILED, STATUS_GENERATING, STATUS_PENDING, Take,
)

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


def test_resolve_enums() -> None:
    # Replicate stores enums as $ref/allOf/anyOf/oneOf into components.schemas, not inline.
    # _resolve_enums must pull them onto the property so the shot editor sees prop['enum'].
    schemas = {
        "resolution": {"type": "string", "enum": ["480p", "720p", "1080p"]},
        "mode": {"type": "string", "enum": ["standard", "pro"]},
        "duration": {"type": "integer", "enum": [5, 10]},
    }
    props = {
        "resolution": {"allOf": [{"$ref": "#/components/schemas/resolution"}], "default": "720p"},
        "mode": {"$ref": "#/components/schemas/mode"},                      # bare $ref
        "duration": {"anyOf": [{"$ref": "#/components/schemas/duration"}]}, # anyOf combiner
        # optional enum: the real Replicate shape for a nullable enum field
        "opt": {"anyOf": [{"$ref": "#/components/schemas/mode"}, {"type": "null"}]},
        "fps": {"oneOf": [{"enum": [16, 24]}]},                            # inline enum in combiner
        "seed": {"type": "integer"},                                       # no enum -> unchanged
        "prompt": {"type": "string"},
    }
    resolved = replicate_client._resolve_enums(props, schemas)
    assert resolved["resolution"]["enum"] == ["480p", "720p", "1080p"]
    assert resolved["resolution"]["default"] == "720p"        # existing keys preserved
    assert resolved["resolution"]["type"] == "string"         # type pulled from the component
    assert resolved["mode"]["enum"] == ["standard", "pro"]
    assert resolved["duration"]["enum"] == [5, 10] and resolved["duration"]["type"] == "integer"
    assert resolved["opt"]["enum"] == ["standard", "pro"]     # enum extracted past the null sibling
    assert resolved["fps"]["enum"] == [16, 24]
    assert "enum" not in resolved["seed"] and "enum" not in resolved["prompt"]
    assert props["resolution"].get("enum") is None            # inputs not mutated
    print("replicate _resolve_enums OK: $ref/allOf/anyOf/oneOf + inline + passthrough, no mutation")


def test_app_settings() -> None:
    from store import app_settings

    saved = paths.APP_SETTINGS
    paths.APP_SETTINGS = Path(tempfile.mkdtemp()) / "app_settings.json"
    try:
        # Default when the file doesn't exist yet (registered default is False).
        assert app_settings.get_bool(app_settings.UPDATE_SCHEMAS_ON_STARTUP) is False
        app_settings.set_bool(app_settings.UPDATE_SCHEMAS_ON_STARTUP, True)
        assert app_settings.get_bool(app_settings.UPDATE_SCHEMAS_ON_STARTUP) is True
        assert paths.APP_SETTINGS.exists()                    # persisted, not just in-memory
        app_settings.set_bool(app_settings.UPDATE_SCHEMAS_ON_STARTUP, False)
        assert app_settings.get_bool(app_settings.UPDATE_SCHEMAS_ON_STARTUP) is False
        assert app_settings.get_bool("nonexistent_key", True) is True   # explicit fallback
    finally:
        paths.APP_SETTINGS = saved
    print("app_settings OK: default, set->get round-trip, persistence, explicit fallback")


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

    # text_encoder_cpu pins CLIP-loader nodes to the CPU (frees ~6GB VRAM on the 12GB card);
    # default leaves the template's device untouched. Node 3 is the CLIPLoader.
    assert wf["3"]["inputs"]["device"] == "default", "default run must not force CPU"
    wf_cpu = comfy_client.prepare_workflow(
        template, start_img=str(a), end_img=str(b), prompt="P", negative="N",
        seed=1, node_roles=roles, text_encoder_cpu=True)
    assert wf_cpu["3"]["inputs"]["device"] == "cpu", "text_encoder_cpu must pin CLIPLoader to cpu"
    # _force_text_encoder_cpu only touches CLIP-loader class types, nothing else.
    assert comfy_client._force_text_encoder_cpu({"x": {"class_type": "KSamplerAdvanced",
                                                       "inputs": {}}}) == 0
    print("comfy prepare_workflow OK: node-role map + heuristic fallback + --set + open-ended sever + cpu-text-enc")


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
    assert "--cache-none" in cmd      # no cross-run model caching -> no VRAM left pinned for spill
    # overriding a default flag drops its value too (no orphaned 8188), keeps the flag
    over = comfy_client.build_launch_command(["--port", "8189"])
    assert "8188" not in over and over[-2:] == ["--port", "8189"]
    assert "--disable-dynamic-vram" in over and "--cache-none" in over
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


def test_cancel_shot_takes() -> None:
    from PySide6.QtWidgets import QApplication

    from backends.jobs import JobManager
    from store.models import STATUS_CANCELLED, STATUS_PENDING

    app = QApplication.instance() or QApplication([])
    project = Project.new()
    shot_a = project.add_shot("kick", model_id="seedance-2.0-std")
    shot_b = project.add_shot("punch", model_id="seedance-2.0-std")
    jm = JobManager(project)

    a1 = project.add_take(shot_a.id, status=STATUS_PENDING)
    a2 = project.add_take(shot_a.id, status=STATUS_PENDING)
    b1 = project.add_take(shot_b.id, status=STATUS_PENDING)

    n = jm.cancel_shot_takes(shot_a.id)
    assert n == 2, n
    assert project.get_take(a1.id).status == STATUS_CANCELLED
    assert project.get_take(a2.id).status == STATUS_CANCELLED
    assert a1.id in jm._cancelled and a2.id in jm._cancelled
    assert project.get_take(b1.id).status == STATUS_PENDING  # other shot untouched
    print("cancel_shot_takes OK: only this shot's queued takes cancelled")


def test_inflight_stop_maps_to_cancelled() -> None:
    # A backend error raised because we asked the render to stop must land the take as
    # CANCELLED, not FAILED. Run the QRunnable directly (no pool) for determinism.
    from PySide6.QtWidgets import QApplication

    from backends.jobs import GenerationJob, JobManager
    from store.models import STATUS_CANCELLED, STATUS_PENDING

    app = QApplication.instance() or QApplication([])
    project = Project.new()
    shot = project.add_shot("kick", model_id="local-flf-wan14b")
    jm = JobManager(project)
    take = project.add_take(shot.id, status=STATUS_PENDING)

    def runner(progress):
        raise RuntimeError("interrupted")   # mimics the backend unwinding after a stop

    jm._stopping.add(take.id)               # mark it as an intentional stop
    job = GenerationJob(project, take.id, "comfyui", runner, jm._signals,
                        jm._cancelled, jm._stopping)
    job.run()
    app.processEvents()

    got = project.get_take(take.id)
    assert got.status == STATUS_CANCELLED, got.status
    assert "stopped by user" in (got.error or "")
    assert take.id not in jm._stopping       # cleared in the finally
    print("inflight stop OK: stop-induced backend error -> CANCELLED, not FAILED")


def test_request_stop_calls_backend() -> None:
    # request_stop must flag the take and issue the right best-effort backend stop, and
    # must swallow a backend that's down (no raise out of a delete). Monkeypatch the two
    # backend stop calls so the test is hermetic - no server, no network, no spend.
    from PySide6.QtWidgets import QApplication

    from backends import comfy_client, replicate_client
    from backends.jobs import JobManager
    from store.models import STATUS_GENERATING, STATUS_PENDING

    app = QApplication.instance() or QApplication([])
    project = Project.new()
    shot = project.add_shot("kick", model_id="seedance-2.0-std")
    jm = JobManager(project)

    calls = []
    saved_stop, saved_cancel = comfy_client.stop_work, replicate_client.cancel_prediction
    comfy_client.stop_work = lambda *a, **k: calls.append(("comfy", a, k))

    def fake_cancel(pred_id, token=None):
        calls.append(("replicate", pred_id))
        raise replicate_client.ReplicateError("server down")   # must be swallowed

    replicate_client.cancel_prediction = fake_cancel
    try:
        # local in-flight take -> comfy interrupt
        lt = project.add_take(shot.id, status=STATUS_GENERATING,
                              settings_snapshot={"backend": "comfyui"})
        assert jm.request_stop(lt.id) is True
        assert lt.id in jm._stopping and ("comfy", (), {}) in calls

        # hosted in-flight take with a recorded prediction id -> replicate cancel (raises,
        # request_stop swallows it)
        ht = project.add_take(shot.id, status=STATUS_GENERATING,
                              settings_snapshot={"backend": "replicate"},
                              backend_job_id="pred_xyz")
        assert jm.request_stop(ht.id) is True
        assert ("replicate", "pred_xyz") in calls

        # a PENDING take is not in-flight -> request_stop no-ops
        pt = project.add_take(shot.id, status=STATUS_PENDING)
        assert jm.request_stop(pt.id) is False
    finally:
        comfy_client.stop_work, replicate_client.cancel_prediction = saved_stop, saved_cancel
    print("request_stop OK: flags take, calls right backend, swallows backend errors")


def test_progress_fraction() -> None:
    from backends.comfy_client import progress_fraction as pf

    assert pf({"type": "progress", "data": {"value": 12, "max": 30, "prompt_id": "p1"}}, "p1") \
        == (0.4, "step 12/30")
    assert pf({"type": "progress", "data": {"value": 1, "max": 2, "prompt_id": "pX"}}, "p1") \
        == (None, "")                                       # different prompt, ignored
    assert pf({"type": "progress", "data": {"value": 5, "max": 10}}, "p1") \
        == (0.5, "step 5/10")                               # legacy message, no prompt_id
    frac, label = pf({"type": "progress_state",
                      "data": {"prompt_id": "p1",
                               "nodes": {"7": {"value": 3, "max": 20},
                                         "8": {"value": 0, "max": 1}}}}, "p1")
    assert (round(frac, 3), label) == (0.15, "step 3/20")   # furthest-along running node
    assert pf({"type": "progress_state",
               "data": {"prompt_id": "p1", "nodes": {"7": {"value": 20, "max": 20}}}}, "p1") \
        == (None, "")                                       # no running node -> no premature 100%
    assert pf({"type": "executing", "data": {"node": None, "prompt_id": "p1"}}, "p1") == (1.0, "")
    assert pf({"type": "progress", "data": {"value": 40, "max": 30}}, "p1") == (1.0, "step 40/30")  # clamp > max
    assert pf({"type": "progress", "data": {"value": 1, "max": 0}}, "p1") == (None, "")  # no div-by-zero
    assert pf({"type": "progress"}, "p1") == (None, "")     # missing data dict
    assert pf({"type": "status", "data": {}}, "p1") == (None, "")
    print("progress_fraction OK: progress/progress_state/executing parsed, prompt_id filtered")


def test_progress_pct() -> None:
    from PySide6.QtWidgets import QApplication

    from backends.jobs import JobManager

    app = QApplication.instance() or QApplication([])
    project = Project.new()
    shot = project.add_shot("kick", model_id="local-flf-wan14b")
    jm = JobManager(project)
    lines, pcts = [], []
    jm.progress.connect(lambda tid, line: lines.append(line))
    jm.progress_pct.connect(lambda tid, frac, label: pcts.append((frac, label)))

    take = project.add_take(shot.id, status=STATUS_PENDING)

    def runner(progress):
        progress("queued abc")                  # milestone: logged
        progress(frac=0.5, label="step 1/2")    # fraction: UI-only, must NOT be logged
        progress(frac=1.0, label="step 2/2")
        return {"video_path": "y.mp4"}

    jm.enqueue(take.id, "comfyui", runner)
    assert jm.wait_for_done(20000), "job did not finish"
    app.processEvents()

    assert (0.5, "step 1/2") in pcts and (1.0, "step 2/2") in pcts
    assert "queued abc" in lines
    assert not any("step" in ln for ln in lines), "pct-only updates must bypass the log signal"
    print("progress_pct OK: fraction signal fires; pct-only updates skip the log")


def test_comfy_views() -> None:
    # history_view / queue_view normalize ComfyUI's /history + /queue into the shape the
    # recovery planner consumes (prompt_id + baked seeds + outputs). Stub _api so no server.
    saved = comfy_client._api
    hist = {
        "P1": {"status": {"status_str": "success", "completed": True},
               "outputs": {"39": {"gifs": [{"filename": "FLF_00006_.mp4", "subfolder": ""}]}},
               "prompt": [5, "P1", {"3": {"class_type": "KSampler",
                                          "inputs": {"noise_seed": 1032416659}}}]},
        "P2": {"status": {"status_str": "error"},
               "outputs": {}, "prompt": [6, "P2", {"3": {"inputs": {"seed": 77}}}]},
    }
    queue = {"queue_running": [[9, "P3", {"3": {"inputs": {"noise_seed": 200}}}]],
             "queue_pending": [[10, "P4", {"3": {"inputs": {"seed": 400}}}]]}

    def fake_api(path, data=None, timeout=30):
        return hist if path == "/history" else queue

    comfy_client._api = fake_api
    try:
        hv = comfy_client.history_view()
        h1 = next(h for h in hv if h["prompt_id"] == "P1")
        assert h1["seeds"] == {1032416659} and h1["ok"] is True
        assert h1["outputs"][-1].name == "FLF_00006_.mp4"
        h2 = next(h for h in hv if h["prompt_id"] == "P2")
        assert h2["ok"] is False and h2["outputs"] == []        # error entry flagged not-ok
        qv = comfy_client.queue_view()
        assert {q["prompt_id"]: q["state"] for q in qv} == {"P3": "running", "P4": "pending"}
        assert next(q for q in qv if q["prompt_id"] == "P3")["seeds"] == {200}
    finally:
        comfy_client._api = saved
    print("comfy history_view/queue_view OK: seeds, outputs, ok-flag, running/pending split")


def test_orphan_recovery() -> None:
    from backends import recovery

    # comfy_orphans selects only mid-flight comfyui takes (generating before pending),
    # ignoring done takes and hosted ones.
    project = Project.new()
    shot = project.add_shot("kick", model_id="local-flf-wan14b")
    snap_local = {"backend": "comfyui"}
    pend = project.add_take(shot.id, status=STATUS_PENDING, seed=400,
                            settings_snapshot=snap_local)
    gen = project.add_take(shot.id, status=STATUS_GENERATING, seed=200,
                           settings_snapshot=snap_local)
    project.add_take(shot.id, status=STATUS_DONE, settings_snapshot=snap_local)      # excluded
    project.add_take(shot.id, status=STATUS_GENERATING, seed=1,                      # excluded:
                     settings_snapshot={"backend": "replicate"})                     # hosted
    orphans = recovery.comfy_orphans(project)
    assert [o.id for o in orphans] == [gen.id, pend.id], "generating-first, hosted/done excluded"

    # plan_comfy_recovery: the four actions + prompt-id match + seed match + claim dedup.
    def t(tid, status, seed=None, job=None):
        return Take(id=tid, shot_id="s", status=status, seed=seed, backend_job_id=job)

    history = [
        {"prompt_id": "Pdone", "seeds": {100}, "outputs": [Path("out/A_00006_.mp4")], "ok": True},
        {"prompt_id": "Pid",   "seeds": {999}, "outputs": [Path("out/B.mp4")], "ok": True},
        {"prompt_id": "Pone",  "seeds": {500}, "outputs": [Path("out/C.mp4")], "ok": True},
    ]
    queue = [{"prompt_id": "Prun", "seeds": {200}, "state": "running"}]
    orphan_list = [
        t("reclaim",  STATUS_GENERATING, seed=100),               # seed -> history -> RECLAIM
        t("reattach", STATUS_GENERATING, seed=200),               # seed -> queue   -> REATTACH
        t("byid",     STATUS_GENERATING, seed=999, job="Pid"),    # prompt-id match -> RECLAIM
        t("dead",     STATUS_GENERATING, seed=300),               # no match (gen)  -> FAIL
        t("nope",     STATUS_PENDING,    seed=400),               # no match (pend) -> CANCEL
        t("dup1",     STATUS_GENERATING, seed=500),               # claims Pone     -> RECLAIM
        t("dup2",     STATUS_GENERATING, seed=500),               # Pone taken      -> FAIL
    ]
    plans = {p.take_id: p for p in recovery.plan_comfy_recovery(orphan_list, history, queue)}
    assert plans["reclaim"].action == recovery.RECLAIM
    assert plans["reclaim"].output_path.endswith("A_00006_.mp4")
    assert plans["reclaim"].prompt_id == "Pdone"
    assert plans["reattach"].action == recovery.REATTACH and plans["reattach"].prompt_id == "Prun"
    assert plans["byid"].action == recovery.RECLAIM and plans["byid"].prompt_id == "Pid"
    assert plans["dead"].action == recovery.FAIL
    assert plans["nope"].action == recovery.CANCEL
    assert plans["dup1"].action == recovery.RECLAIM and plans["dup2"].action == recovery.FAIL, \
        "a finished render must be claimed by exactly one take"
    print("orphan recovery OK: select + reclaim/reattach/fail/cancel + prompt-id + seed dedup")


if __name__ == "__main__":
    test_build_input()
    test_capability_sync()
    test_resolve_enums()
    test_app_settings()
    test_roster_integrity()
    test_comfy_prepare()
    test_dynamic_vram_gate()
    test_comfy_launch_helpers()
    test_comfy_stop_helpers()
    test_comfy_views()
    test_orphan_recovery()
    test_total_price()
    test_cost_summary()
    test_cancel_pending()
    test_cancel_shot_takes()
    test_inflight_stop_maps_to_cancelled()
    test_request_stop_calls_backend()
    test_job_manager()
    test_progress_fraction()
    test_progress_pct()
    print("PHASE 2 SMOKE: PASS")
