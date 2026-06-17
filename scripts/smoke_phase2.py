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

    # Async weight offloading is a SECOND streaming path: default-ON on a GPU (device.type
    # 'cuda'), independent of dynamic VRAM, off only via --disable-async-offload / --cpu / 0.
    ao = comfy_client.async_offload_enabled
    assert ao(base, "cuda") is True                      # default-on on a GPU
    assert ao(base, "cpu") is False                      # never on a CPU device
    assert ao(base, None) is True                        # unknown device -> conservative refuse
    assert ao(base + ["--disable-async-offload"], "cuda") is False
    assert ao(base + ["--cpu"], "cuda") is False
    assert ao(base + ["--async-offload", "0"], "cuda") is False  # explicit 0 -> off
    assert ao(base + ["--async-offload", "4"], "cpu") is True     # explicit count -> on
    # mirrors ComfyUI's own `if NUM_STREAMS > 0` gate: a non-positive count never streams
    assert ao(base + ["--async-offload", "-1"], "cuda") is False
    assert ao(base + ["--async-offload"], "cuda") is True         # bare flag -> const 2
    assert ao(base + ["--async-offload=0"], "cuda") is False
    # --disable-dynamic-vram alone does NOT turn async offload off (the actual TDR bug).
    assert ao(base + ["--disable-dynamic-vram"], "cuda") is True
    print("comfy weight-streaming gate OK: dynamic-VRAM + async-offload, each disabling flag")


def test_preflight_gate() -> None:
    # preflight() must refuse if EITHER streaming path is active; pass only when both are off.
    # Stub _api so no real server is needed; restore it afterward.
    saved_api = comfy_client._api
    base = ["main.py", "--listen", "127.0.0.1", "--port", "8188"]

    def fake_stats(argv, device_type="cuda"):
        return lambda path, *a, **k: {"system": {"argv": argv},
                                      "devices": [{"type": device_type}]}
    try:
        # aimdo off but async offload still on (the 2026-06-17 bug) -> still refused
        comfy_client._api = fake_stats(base + ["--disable-dynamic-vram"])
        try:
            comfy_client.preflight()
            assert False, "preflight should refuse a server still doing async offload"
        except comfy_client.ComfyError as e:
            assert "async weight offloading" in str(e)
        # both streaming paths off -> passes
        comfy_client._api = fake_stats(base + ["--disable-dynamic-vram", "--disable-async-offload"])
        comfy_client.preflight()
        # unknown device type (None) is the safety-critical reading: async offload assumed on
        comfy_client._api = fake_stats(base + ["--disable-dynamic-vram"], device_type=None)
        try:
            comfy_client.preflight()
            assert False, "preflight should refuse when the device type is unknown (assume GPU)"
        except comfy_client.ComfyError as e:
            assert "async weight offloading" in str(e)
        # dynamic VRAM on -> refused
        comfy_client._api = fake_stats(base + ["--disable-async-offload"])
        try:
            comfy_client.preflight()
            assert False, "preflight should refuse a server with dynamic VRAM on"
        except comfy_client.ComfyError as e:
            assert "dynamic VRAM" in str(e)
        # bypass env neutralizes the guard even with everything on
        os.environ["ANIMGEN_ALLOW_DYNAMIC_VRAM"] = "1"
        comfy_client._api = fake_stats(base)              # both on
        comfy_client.preflight()
    finally:
        os.environ.pop("ANIMGEN_ALLOW_DYNAMIC_VRAM", None)
        comfy_client._api = saved_api
    print("comfy preflight gate OK: refuses dynamic-VRAM OR async-offload, bypass honored")


def test_comfy_launch_helpers() -> None:
    cmd = comfy_client.build_launch_command()
    assert cmd[1].endswith("main.py")
    assert "--disable-dynamic-vram" in cmd and "--port" in cmd
    assert "--disable-async-offload" in cmd  # the second PCIe weight-streaming path, also off
    assert "--cache-none" in cmd      # no cross-run model caching -> no VRAM left pinned for spill
    # overriding a default flag drops its value too (no orphaned 8188), keeps the flag
    over = comfy_client.build_launch_command(["--port", "8189"])
    assert "8188" not in over and over[-2:] == ["--port", "8189"]
    assert "--disable-dynamic-vram" in over and "--disable-async-offload" in over
    assert "--cache-none" in over
    # status probe is non-raising and well-shaped whether or not a server is up
    st = comfy_client.server_status(timeout=1)
    assert set(st) == {"running", "version", "dynamic_vram", "async_offload", "argv"}
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
    assert got_ok.started, "run() must stamp `started` at the GENERATING transition"
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
                        jm._cancelled, jm._stopping, jm._requeue, jm._on_job_done)
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


def test_is_stop_requested() -> None:
    # request_stop on a hosted take whose create-POST hasn't returned (no backend_job_id):
    # it flags the take and is_stop_requested reports True, but sends no replicate cancel
    # (there's no prediction id to cancel yet) - the runner's on_submit closes that window.
    from PySide6.QtWidgets import QApplication

    from backends import replicate_client
    from backends.jobs import JobManager
    from store.models import STATUS_GENERATING

    app = QApplication.instance() or QApplication([])
    project = Project.new()
    shot = project.add_shot("kick", model_id="seedance-2.0-std")
    jm = JobManager(project)

    calls = []
    saved = replicate_client.cancel_prediction
    replicate_client.cancel_prediction = lambda *a, **k: calls.append(a)
    try:
        ht = project.add_take(shot.id, status=STATUS_GENERATING,
                              settings_snapshot={"backend": "replicate"})  # no backend_job_id
        assert jm.is_stop_requested(ht.id) is False
        assert jm.request_stop(ht.id) is True
        assert jm.is_stop_requested(ht.id) is True
        assert calls == []   # no cancel sent - the prediction id isn't recorded yet
    finally:
        replicate_client.cancel_prediction = saved
    print("is_stop_requested OK: flags take during create-POST window, no premature cancel")


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


def test_client_id_in_queue() -> None:
    from backends.comfy_client import _client_id_in_queue as cid

    # entry shape: [number, prompt_id, prompt, extra_data{client_id}, outputs]
    q = {"queue_running": [[2, "p1", {"...": "wf"}, {"client_id": "abc", "create_time": 1}, ["16"]]],
         "queue_pending": [[3, "p2", {"...": "wf"}, {"client_id": "def"}, ["16"]]]}
    assert cid(q, "p1") == "abc"                         # found in running bucket
    assert cid(q, "p2") == "def"                         # found in pending bucket
    assert cid(q, "p3") is None                          # absent prompt
    assert cid({}, "p1") is None                         # empty payload
    assert cid({"queue_running": [[2, "p1", {"x": 1}, ["16"]]]}, "p1") is None  # no extra_data dict
    assert cid({"queue_running": [[2, "p1", {"x": 1}, {"create_time": 1}, []]]}, "p1") is None  # no client_id key
    print("client_id_in_queue OK: running/pending lookup, missing extra_data/client_id tolerated")


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


def test_crash_recovery() -> None:
    from backends.crash_recovery import (QueueAbandoned, _looks_crashed, format_elapsed,
                                         run_with_crash_recovery)

    # format_elapsed: compact span, clamps negatives.
    assert format_elapsed(45) == "45s"
    assert format_elapsed(75) == "1m15s"
    assert format_elapsed(3675) == "1h1m15s"
    assert format_elapsed(-5) == "0s"

    # _looks_crashed: an all-down server is probed exactly `probes` times (bounded), the first
    # "up" is trusted after a single probe, and `probes <= 0` still floors at one probe.
    calls = [0]
    def down():
        calls[0] += 1
        return False
    assert _looks_crashed(down, 3) is True and calls[0] == 3
    calls[0] = 0
    def up():
        calls[0] += 1
        return True
    assert _looks_crashed(up, 3) is False and calls[0] == 1
    calls[0] = 0
    assert _looks_crashed(down, 0) is True and calls[0] == 1

    # A deterministic fake clock: each read advances 1s (so an attempt "takes" 1s).
    def make_clock():
        ticks = [0.0]
        def clock():
            ticks[0] += 1.0
            return ticks[0]
        return clock

    # (a) success on the first try -> no restart, no abandon, server never consulted.
    notes, restarts, abandons = [], [], []
    res = run_with_crash_recovery(
        render=lambda: {"video_path": "ok.mp4"},
        server_running=lambda: True, restart_server=lambda: restarts.append(1),
        note=notes.append, on_abandon=abandons.append, clock=make_clock())
    assert res == {"video_path": "ok.mp4"} and not restarts and not abandons

    # (b) crash once (server down after the failure) then succeed -> 1 restart, "attempt 2/3".
    attempts = [0]
    def render_crash_then_ok():
        attempts[0] += 1
        if attempts[0] == 1:
            raise comfy_client.ComfyError("ComfyUI unreachable")
        return {"video_path": "recovered.mp4"}
    notes, restarts, abandons = [], [], []
    res = run_with_crash_recovery(
        render=render_crash_then_ok, server_running=lambda: False,
        restart_server=lambda: restarts.append(1),
        note=notes.append, on_abandon=abandons.append, clock=make_clock())
    assert res == {"video_path": "recovered.mp4"}
    assert len(restarts) == 1 and not abandons
    assert any("retrying (attempt 2/3)" in n and "failed in" in n for n in notes), notes

    # (c) crash every time -> QueueAbandoned after 3 tries, on_abandon called once, 2 restarts.
    notes, restarts, abandons = [], [], []
    def always_crash():
        raise comfy_client.ComfyError("ComfyUI unreachable")
    try:
        run_with_crash_recovery(
            render=always_crash, server_running=lambda: False,
            restart_server=lambda: restarts.append(1),
            note=notes.append, on_abandon=abandons.append, clock=make_clock())
        assert False, "expected QueueAbandoned"
    except QueueAbandoned:
        pass
    assert len(restarts) == 2 and len(abandons) == 1
    assert "crashed 3x" in abandons[0] and "pausing the local queue" in abandons[0]

    # (d) failure with the server still UP -> genuine workflow error, propagates unchanged.
    notes, restarts, abandons = [], [], []
    def workflow_error():
        raise comfy_client.ComfyError("workflow error: bad node")
    try:
        run_with_crash_recovery(
            render=workflow_error, server_running=lambda: True,
            restart_server=lambda: restarts.append(1),
            note=notes.append, on_abandon=abandons.append, clock=make_clock())
        assert False, "expected the workflow error to propagate"
    except comfy_client.ComfyError as e:
        assert "workflow error" in str(e) and not isinstance(e, QueueAbandoned)
    assert not restarts and not abandons, "a server-up failure must not restart or abandon"

    # (e) restart itself fails -> QueueAbandoned + on_abandon (can't recover without a server).
    notes, restarts, abandons = [], [], []
    def restart_boom():
        raise comfy_client.ComfyError("did not come back up")
    try:
        run_with_crash_recovery(
            render=always_crash, server_running=lambda: False, restart_server=restart_boom,
            note=notes.append, on_abandon=abandons.append, clock=make_clock())
        assert False, "expected QueueAbandoned"
    except QueueAbandoned:
        pass
    assert len(abandons) == 1 and "restart failed" in abandons[0]

    # (f) hardened crash signal: a single transient "down" that re-probes "up" is a workflow
    # error, not a crash -> the failure propagates and no restart fires. We trust the first
    # "up" reading, so the common workflow-error path still consults the server exactly once.
    probe_seq = [False, True]   # first probe down (the race), second probe up (really alive)
    probe_calls = [0]
    def flaky_server_running():
        probe_calls[0] += 1
        return probe_seq.pop(0) if probe_seq else True
    notes, restarts, abandons = [], [], []
    try:
        run_with_crash_recovery(
            render=workflow_error, server_running=flaky_server_running,
            restart_server=lambda: restarts.append(1),
            note=notes.append, on_abandon=abandons.append, clock=make_clock())
        assert False, "expected the workflow error to propagate"
    except comfy_client.ComfyError as e:
        assert not isinstance(e, QueueAbandoned)
    assert not restarts and not abandons, "a transient down that re-probes up must not restart"
    assert probe_calls[0] == 2, "should keep probing until it sees the server is up"

    # ...and a server up on the very first probe is consulted exactly once (no slowdown).
    one_probe = [0]
    def up_once():
        one_probe[0] += 1
        return True
    try:
        run_with_crash_recovery(
            render=workflow_error, server_running=up_once,
            restart_server=lambda: None, note=lambda *_: None,
            on_abandon=lambda *_: None, clock=make_clock())
        assert False, "expected the workflow error to propagate"
    except comfy_client.ComfyError:
        pass
    assert one_probe[0] == 1, "common workflow-error path must probe the server only once"

    # (g) should_abort True (user paused / deliberately stopped ComfyUI): a render failure is
    # NOT a crash to restart even with the server down - re-raise verbatim, never restart or
    # abandon (the holding is done by JobManager.pause_local clearing the pool).
    notes, restarts, abandons = [], [], []
    probed = [0]
    def server_probe():
        probed[0] += 1
        return False
    try:
        run_with_crash_recovery(
            render=always_crash, server_running=server_probe,
            restart_server=lambda: restarts.append(1),
            note=notes.append, on_abandon=abandons.append,
            should_abort=lambda: True, clock=make_clock())
        assert False, "expected the failure to propagate when should_abort is True"
    except comfy_client.ComfyError as e:
        assert not isinstance(e, QueueAbandoned)
    assert not restarts and not abandons, "a deliberate user stop must not restart or abandon"
    assert probed[0] == 0, "should_abort short-circuits before the crash probe"
    print("crash_recovery OK: success/retry/abandon/workflow-error/restart-fail/"
          "transient-down/user-abort + format_elapsed")


def test_wait_until_responsive() -> None:
    # Polls server_status() until it reports running, or times out. Stub server_status so no
    # real socket/server; poll_s=0 keeps the between-probe time.sleep(0) effectively instant.
    saved_status = comfy_client.server_status
    try:
        calls = [0]
        def flips_running(timeout=2):
            calls[0] += 1
            return {"running": calls[0] >= 3}        # down twice, then up
        comfy_client.server_status = flips_running
        assert comfy_client.wait_until_responsive(timeout_s=60, poll_s=0.0) is True
        assert calls[0] == 3

        comfy_client.server_status = lambda timeout=2: {"running": False}  # never comes up
        assert comfy_client.wait_until_responsive(timeout_s=0, poll_s=0.0) is False

        # is_alive=False short-circuits: a dead process bails out before the timeout elapses.
        probes = [0]
        def down(timeout=2):
            probes[0] += 1
            return {"running": False}
        comfy_client.server_status = down
        assert comfy_client.wait_until_responsive(
            timeout_s=600, poll_s=0.0, is_alive=lambda: False) is False
        assert probes[0] == 1, "a dead process should stop polling after the first probe"
    finally:
        comfy_client.server_status = saved_status
    print("wait_until_responsive OK: returns on running, False on timeout/dead-process")


def test_restart_server() -> None:
    # restart_server orchestration: stop (tolerating ComfyError) -> settle -> launch -> wait.
    # Stub all three so no real process/socket; verify call order and the failure messages.
    # settle_s=0 skips the real port-release sleep; launch returns a fake Popen whose poll()
    # liveness drives the fast-fail path.
    class FakeProc:
        def __init__(self, returncode=None):
            self.returncode = returncode
        def poll(self):
            return self.returncode      # None == still alive, int == already exited
    saved = (comfy_client.stop_server, comfy_client.launch_server,
             comfy_client.wait_until_responsive)
    order = []
    try:
        def stop():
            order.append("stop")
            raise comfy_client.ComfyError("nothing to stop")   # must be swallowed
        comfy_client.stop_server = stop
        comfy_client.launch_server = lambda extra=None: order.append("launch") or FakeProc()
        comfy_client.wait_until_responsive = lambda *a, **k: True
        comfy_client.restart_server(settle_s=0)
        assert order == ["stop", "launch"], order

        # server never answers but the process is still alive -> "did not come back up".
        comfy_client.wait_until_responsive = lambda *a, **k: False
        try:
            comfy_client.restart_server(ready_timeout_s=5, settle_s=0)
            assert False, "expected ComfyError when the server doesn't come back"
        except comfy_client.ComfyError as e:
            assert "did not come back up" in str(e)

        # the relaunched process exited at once (e.g. lost the port bind) -> fast-fail message.
        comfy_client.launch_server = lambda extra=None: FakeProc(returncode=1)
        try:
            comfy_client.restart_server(ready_timeout_s=5, settle_s=0)
            assert False, "expected ComfyError when the relaunched process exits immediately"
        except comfy_client.ComfyError as e:
            assert "exited immediately" in str(e)
    finally:
        (comfy_client.stop_server, comfy_client.launch_server,
         comfy_client.wait_until_responsive) = saved
    print("restart_server OK: stop(tolerant)->settle->launch->wait; "
          "raises unresponsive/exited-immediately")


def test_ensure_server() -> None:
    # ensure_server: cold-start ComfyUI before a render if it's down, no-op if already up.
    # Stub server_status / launch_server / wait_until_responsive so there's no real
    # process/socket; verify the no-launch, launch, and two failure paths.
    class FakeProc:
        def __init__(self, returncode=None):
            self.returncode = returncode
        def poll(self):
            return self.returncode      # None == still alive, int == already exited

    saved = (comfy_client.server_status, comfy_client.launch_server,
             comfy_client.wait_until_responsive)
    launched = []
    try:
        # already running -> returns True, never launches.
        comfy_client.server_status = lambda timeout=2: {"running": True}
        comfy_client.launch_server = lambda extra=None: launched.append("launch") or FakeProc()
        comfy_client.wait_until_responsive = lambda *a, **k: True
        assert comfy_client.ensure_server() is True
        assert launched == [], launched

        # down -> launches, waits, returns False.
        comfy_client.server_status = lambda timeout=2: {"running": False}
        assert comfy_client.ensure_server() is False
        assert launched == ["launch"], launched

        # launched but never answers while the process stays alive -> "did not become responsive".
        comfy_client.wait_until_responsive = lambda *a, **k: False
        launched.clear()
        try:
            comfy_client.ensure_server(ready_timeout_s=5)
            assert False, "expected ComfyError when the launched server never answers"
        except comfy_client.ComfyError as e:
            assert "did not become responsive" in str(e)
        assert launched == ["launch"], launched   # it did attempt a launch first

        # launched process exited at once (e.g. lost the port bind) -> fast-fail message.
        comfy_client.launch_server = lambda extra=None: FakeProc(returncode=1)
        try:
            comfy_client.ensure_server(ready_timeout_s=5)
            assert False, "expected ComfyError when the launched process exits immediately"
        except comfy_client.ComfyError as e:
            assert "exited immediately" in str(e)
    finally:
        (comfy_client.server_status, comfy_client.launch_server,
         comfy_client.wait_until_responsive) = saved
    print("ensure_server OK: no-op when up; launch+wait when down; "
          "raises unresponsive/exited-immediately")


def test_abandon_local() -> None:
    import threading
    import time

    from PySide6.QtWidgets import QApplication

    from backends.jobs import JobManager
    from store.models import STATUS_CANCELLED, STATUS_DONE, STATUS_GENERATING

    app = QApplication.instance() or QApplication([])
    project = Project.new()
    shot = project.add_shot("kick", model_id="local-flf-wan14b")
    jm = JobManager(project)
    abandoned = []
    jm.queue_abandoned.connect(abandoned.append)

    local_snap, hosted_snap = {"backend": "comfyui"}, {"backend": "replicate"}
    release = threading.Event()
    active = project.add_take(shot.id, status=STATUS_PENDING, settings_snapshot=local_snap)
    lq1 = project.add_take(shot.id, status=STATUS_PENDING, settings_snapshot=local_snap)
    lq2 = project.add_take(shot.id, status=STATUS_PENDING, settings_snapshot=local_snap)
    hosted = project.add_take(shot.id, status=STATUS_PENDING, settings_snapshot=hosted_snap)

    def blocker(progress):           # occupies the single local worker until released
        release.wait(timeout=10)
        return {"video_path": "x.mp4"}

    jm.enqueue(active.id, "comfyui", blocker)
    jm.enqueue(lq1.id, "comfyui", blocker)   # never runs - cleared/cancelled by abandon_local
    jm.enqueue(lq2.id, "comfyui", blocker)
    jm.enqueue(hosted.id, "replicate", lambda p: {"video_path": "h.mp4"})  # hosted pool

    for _ in range(100):             # wait until the local blocker is actually generating
        if project.get_take(active.id).status == STATUS_GENERATING:
            break
        time.sleep(0.02)
    assert project.get_take(active.id).status == STATUS_GENERATING, \
        "local blocker never started; abandon would wrongly cancel it as pending"

    n = jm.abandon_local("ComfyUI crashed 3x; pausing the local queue.")
    assert n == 2, n                 # the two queued LOCAL takes, not the hosted one
    assert project.get_take(lq1.id).status == STATUS_CANCELLED
    assert project.get_take(lq2.id).status == STATUS_CANCELLED
    assert abandoned == ["ComfyUI crashed 3x; pausing the local queue."]

    release.set()
    assert jm.wait_for_done(10000), "jobs did not finish"
    app.processEvents()
    assert project.get_take(active.id).status == STATUS_DONE   # the running local one untouched
    assert project.get_take(hosted.id).status == STATUS_DONE   # hosted take untouched
    print("abandon_local OK: local pending cancelled, running + hosted untouched, signal fired")


def test_pause_resume_local() -> None:
    """Pause holds queued local takes (kept PENDING, not cancelled) and flips is_local_paused;
    resume re-enqueues them with their original runners. The running local take is left to
    finish (card #41 'pause after current')."""
    import threading
    import time

    from PySide6.QtWidgets import QApplication

    from backends.jobs import JobManager
    from store.models import STATUS_DONE, STATUS_GENERATING, STATUS_PENDING

    app = QApplication.instance() or QApplication([])
    project = Project.new()
    shot = project.add_shot("kick", model_id="local-flf-wan14b")
    jm = JobManager(project)

    local_snap = {"backend": "comfyui"}
    gate1, gate2 = threading.Event(), threading.Event()
    active = project.add_take(shot.id, status=STATUS_PENDING, settings_snapshot=local_snap)
    q1 = project.add_take(shot.id, status=STATUS_PENDING, settings_snapshot=local_snap)
    q2 = project.add_take(shot.id, status=STATUS_PENDING, settings_snapshot=local_snap)

    runs = {"count": 0}
    def active_runner(progress):
        runs["count"] += 1
        gate1.wait(timeout=10)
        return {"video_path": "active.mp4"}
    def quick(progress):
        gate2.wait(timeout=10)
        return {"video_path": "q.mp4"}

    jm.enqueue(active.id, "comfyui", active_runner)
    jm.enqueue(q1.id, "comfyui", quick)
    jm.enqueue(q2.id, "comfyui", quick)

    for _ in range(100):
        if project.get_take(active.id).status == STATUS_GENERATING:
            break
        time.sleep(0.02)
    assert project.get_take(active.id).status == STATUS_GENERATING

    held = jm.pause_local()              # "pause after current": hold the two queued, not active
    assert jm.is_local_paused() is True
    assert set(held) == {q1.id, q2.id}, held
    assert project.get_take(q1.id).status == STATUS_PENDING   # held, NOT cancelled
    assert project.get_take(q2.id).status == STATUS_PENDING

    gate1.set()                          # let the active take finish (it was untouched)
    for _ in range(100):
        if project.get_take(active.id).status == STATUS_DONE:
            break
        time.sleep(0.02)
    app.processEvents()
    assert project.get_take(active.id).status == STATUS_DONE
    # While paused the held takes stay queued and never start.
    time.sleep(0.1)
    assert project.get_take(q1.id).status == STATUS_PENDING

    gate2.set()                          # unblock the held runners for when they run
    n = jm.resume_local(held)            # re-enqueue both held takes with their original runners
    assert n == 2 and jm.is_local_paused() is False
    assert jm.wait_for_done(10000), "resumed jobs did not finish"
    app.processEvents()
    assert project.get_take(q1.id).status == STATUS_DONE
    assert project.get_take(q2.id).status == STATUS_DONE
    print("pause_resume_local OK: held PENDING, active finished, resume re-ran the held takes")


def test_pause_requeue_current() -> None:
    """Pause with requeue_current halts the in-flight local take and resets it to PENDING
    (not terminal) so resume re-runs it from scratch (card #41 'halt current & re-add')."""
    import threading
    import time

    from PySide6.QtWidgets import QApplication

    from backends import comfy_client
    from backends.jobs import JobManager
    from store.models import STATUS_GENERATING, STATUS_PENDING

    app = QApplication.instance() or QApplication([])
    project = Project.new()
    shot = project.add_shot("kick", model_id="local-flf-wan14b")
    jm = JobManager(project)

    # stop_and_requeue calls comfy_client.stop_work(); stub it so no real server is touched and
    # make the interrupt actually unblock the runner (mimicking ComfyUI aborting the prompt).
    interrupted = threading.Event()
    orig_stop_work = comfy_client.stop_work
    comfy_client.stop_work = lambda: interrupted.set()  # type: ignore[assignment]
    try:
        local_snap = {"backend": "comfyui"}
        active = project.add_take(shot.id, status=STATUS_PENDING, settings_snapshot=local_snap)

        attempts = {"n": 0}
        def runner(progress):
            attempts["n"] += 1
            if attempts["n"] == 1:                 # first run is interrupted by the requeue stop
                interrupted.wait(timeout=10)
                raise comfy_client.ComfyError("interrupted")
            return {"video_path": "rerun.mp4"}     # the resumed run succeeds

        jm.enqueue(active.id, "comfyui", runner)
        for _ in range(100):
            if project.get_take(active.id).status == STATUS_GENERATING:
                break
            time.sleep(0.02)
        assert project.get_take(active.id).status == STATUS_GENERATING

        held = jm.pause_local(requeue_current=True)
        assert active.id in held, held
        for _ in range(100):                       # worker unwinds the interrupted render to PENDING
            if project.get_take(active.id).status == STATUS_PENDING:
                break
            time.sleep(0.02)
        app.processEvents()
        assert project.get_take(active.id).status == STATUS_PENDING, "halted take must be re-queued"

        n = jm.resume_local(held)                  # re-runs the same take; second attempt succeeds
        assert n == 1
        assert jm.wait_for_done(10000)
        app.processEvents()
        t = project.get_take(active.id)
        assert t.status == STATUS_DONE and t.video_path == "rerun.mp4", (t.status, t.video_path)
        assert attempts["n"] == 2, "the re-added take must run a second time"
    finally:
        comfy_client.stop_work = orig_stop_work  # type: ignore[assignment]
    print("pause_requeue_current OK: in-flight take halted, reset PENDING, re-ran on resume")


def test_batch() -> None:
    from backends import batch

    class _Shot:
        def __init__(self, name, model_id, start_frame="a.png", aspect=None, settings=None):
            self.name = name
            self.model_id = model_id
            self.start_frame = start_frame
            self.crop = {"aspect": aspect} if aspect else {}
            self.settings = settings or {}

    models = {
        "good": {"display_name": "Good", "backend": "replicate",
                 "default_params": {"duration": 4}},
        "local": {"display_name": "Local", "backend": "comfyui", "default_params": {}},
    }
    aspects = {"good": ["1:1", "16:9"], "local": ["1:1"]}
    model_of = lambda s: models.get(s.model_id)              # noqa: E731
    aspects_of = lambda mid: aspects.get(mid, [])            # noqa: E731
    est_of = lambda mid, settings: None if mid == "local" else 0.5   # noqa: E731

    shots = [
        _Shot("ok1", "good", aspect="1:1"),
        _Shot("ok2", "local", aspect="1:1"),
        _Shot("bad_model", "nope"),
        _Shot("bad_aspect", "good", aspect="9:21"),
        _Shot("no_frame", "good", start_frame=""),
    ]
    plan = batch.plan_batch(shots, takes_per_shot=3, model_of=model_of,
                            aspects_of=aspects_of, est_of=est_of)
    assert len(plan.eligible) == 2, plan.eligible
    assert plan.take_count == 6 and len(plan.items) == 6
    it = plan.items[0]
    assert set(it) >= {"name", "model_display", "backend", "est_cost", "params"}
    assert {n for n, _ in plan.skipped} == {"bad_model", "bad_aspect", "no_frame"}
    ok1_item = next(i for i in plan.items if i["name"] == "ok1")
    assert ok1_item["params"].get("duration") == 4   # default_params merged into settings

    # takes_per_shot floors at 1
    assert batch.plan_batch([shots[0]], takes_per_shot=0, model_of=model_of,
                            aspects_of=aspects_of, est_of=est_of).take_count == 1

    # BatchRun completion: terminal-only, complete when all takes drained
    run = batch.BatchRun(take_ids={"a", "b"}, power_action=batch.POWER_NONE, started="t0")
    assert not run.complete
    run.mark("a", "generating")          # non-terminal -> ignored
    assert run.remaining == {"a", "b"}
    run.mark("a", "done")
    run.mark("x", "done")                # not in batch -> ignored
    assert run.remaining == {"b"} and not run.complete
    run.mark("b", "cancelled")
    assert run.complete

    rows = [{"name": "ok1", "status": "done", "cost_actual": 0.5},
            {"name": "ok2", "status": "failed", "cost_actual": None},
            {"name": "ok3", "status": "cancelled", "cost_actual": None},
            {"name": "ok4", "status": "generating", "cost_actual": None}]  # non-canonical branch
    rep = batch.build_batch_report(rows, started="t0", finished="t1",
                                   power_action=batch.POWER_SLEEP)
    for token in ("done", "failed", "cancelled", "generating", "$0.50", "sleep", "t0", "t1"):
        assert token in rep, token

    cmd = batch.sleep_command()
    assert cmd and isinstance(cmd, list)
    print("batch OK: plan eligibility/N-per-shot, BatchRun completion, report, sleep cmd")


def test_batch_finalize() -> None:
    import tempfile
    from pathlib import Path

    from PySide6.QtWidgets import QApplication

    from PySide6.QtWidgets import QMessageBox

    import paths
    from backends import batch
    from store.models import STATUS_CANCELLED
    from ui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])  # noqa: F841
    reports_dir = Path(tempfile.mkdtemp())
    orig_exports, paths.EXPORTS_DIR = paths.EXPORTS_DIR, reports_dir
    # _on_queue_abandoned pops a modal warning (correct in the real GUI); stub it so the
    # headless test doesn't block on .exec() (hard-won rule 4).
    orig_warn = QMessageBox.warning
    QMessageBox.warning = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok)
    try:
        project = Project.new()
        shot = project.add_shot("kick", model_id="seedance-2.0-std")
        t1 = project.add_take(shot.id, status=STATUS_PENDING)
        t2 = project.add_take(shot.id, status=STATUS_PENDING)
        win = MainWindow(project)

        fired = []
        win._perform_power_action = lambda action: fired.append(action)
        win._batch = batch.BatchRun(take_ids={t1.id, t2.id},
                                    power_action=batch.POWER_SLEEP, started="t0")

        project.update_take(t1.id, status=STATUS_DONE)
        win._on_status_changed(t1.id, STATUS_DONE)
        assert win._batch is not None and not fired   # one still pending -> no finalize yet

        project.update_take(t2.id, status=STATUS_FAILED)
        win._on_status_changed(t2.id, STATUS_FAILED)
        assert win._batch is None, "batch should clear once all takes terminal"
        assert fired == [batch.POWER_SLEEP], fired
        written = list(reports_dir.glob("overnight_*.txt"))
        assert len(written) == 1, written
        body = written[0].read_text(encoding="utf-8")
        assert "done" in body and "failed" in body and "kick" in body

        # queue_abandoned mid-batch neutralizes the power action but still finalizes/reports.
        project2 = Project.new()
        s2 = project2.add_shot("punch", model_id="seedance-2.0-std")
        a1 = project2.add_take(s2.id, status=STATUS_PENDING)
        win2 = MainWindow(project2)
        fired2 = []
        win2._perform_power_action = lambda action: fired2.append(action)
        win2._batch = batch.BatchRun(take_ids={a1.id},
                                     power_action=batch.POWER_SLEEP, started="t0")
        win2._on_queue_abandoned("crashed; pausing")
        assert win2._batch is not None and win2._batch.power_action == batch.POWER_NONE
        project2.update_take(a1.id, status=STATUS_CANCELLED)
        win2._on_status_changed(a1.id, STATUS_CANCELLED)
        assert win2._batch is None and fired2 == [], "abandon must neutralize the power action"
        assert len(list(reports_dir.glob("overnight_*.txt"))) == 2  # report still written
    finally:
        paths.EXPORTS_DIR = orig_exports
        QMessageBox.warning = orig_warn
    print("batch finalize OK: drain->report+power, partial pending no-op, abandon neutralizes")


def test_done_elapsed() -> None:
    from ui.queue_view import done_elapsed
    from store.models import Take

    # The fix: "done in X" is render duration (started -> completed), NOT queue wait
    # (created -> completed). The serialized local queue stamps every take's `created` at
    # batch launch, so a late take's created->completed is its cumulative wait (e.g. 1h34m),
    # not the ~6m it actually rendered.
    t = Take(id="t", shot_id="s",
             created="2026-06-17T19:06:00",        # queued at batch launch
             started="2026-06-17T20:35:00",        # began rendering ~1.5h later
             completed="2026-06-17T20:40:50")
    assert done_elapsed(t) == "5m50s", done_elapsed(t)
    # Legacy take generated before `started` existed: fall back to created -> completed.
    legacy = Take(id="t2", shot_id="s",
                  created="2026-06-17T20:35:00", completed="2026-06-17T20:35:30")
    assert done_elapsed(legacy) == "30s", done_elapsed(legacy)
    # No completed yet -> "" so the caller shows a bare "done".
    assert done_elapsed(Take(id="t3", shot_id="s", created="2026-06-17T20:35:00")) == ""

    # `started` round-trips through takes.json, and a dict lacking it (old file) loads as None.
    proj = Project.new()
    sh = proj.add_shot("idle", model_id="seedance-2.0-std")
    tk = proj.add_take(sh.id, status=STATUS_PENDING)
    proj.update_take(tk.id, started="2026-06-17T20:35:00")
    d = proj._take_to_dict(proj.get_take(tk.id))
    assert d["started"] == "2026-06-17T20:35:00"
    assert proj._take_from_dict(d).started == "2026-06-17T20:35:00"
    d.pop("started")
    assert proj._take_from_dict(d).started is None
    print("done_elapsed OK: render time from started, created fallback, started persists")


if __name__ == "__main__":
    test_build_input()
    test_capability_sync()
    test_resolve_enums()
    test_app_settings()
    test_roster_integrity()
    test_comfy_prepare()
    test_dynamic_vram_gate()
    test_preflight_gate()
    test_comfy_launch_helpers()
    test_comfy_stop_helpers()
    test_comfy_views()
    test_orphan_recovery()
    test_crash_recovery()
    test_wait_until_responsive()
    test_restart_server()
    test_ensure_server()
    test_abandon_local()
    test_pause_resume_local()
    test_pause_requeue_current()
    test_total_price()
    test_cost_summary()
    test_cancel_pending()
    test_cancel_shot_takes()
    test_inflight_stop_maps_to_cancelled()
    test_request_stop_calls_backend()
    test_is_stop_requested()
    test_job_manager()
    test_progress_fraction()
    test_client_id_in_queue()
    test_progress_pct()
    test_done_elapsed()
    test_batch()
    test_batch_finalize()
    print("PHASE 2 SMOKE: PASS")
