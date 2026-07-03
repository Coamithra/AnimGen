"""Phase 2 smoke test (no spend, no ComfyUI required).

Covers: hosted field mapping (build_input), local workflow prep (node-role + heuristic
+ --set), cost-summary math, and the JobManager driving a fake runner through
pending -> generating -> done and the failure path.

    QT_QPA_PLATFORM=offscreen PYTHONIOENCODING=utf-8 \
        animgen/.venv/Scripts/python.exe animgen/scripts/smoke_phase2.py
"""
from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
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


def test_store_absent_vs_unreadable() -> None:
    """M11: the three app-global store loaders must distinguish an ABSENT file (seeds/
    defaults are correct) from a PRESENT-BUT-UNREADABLE one (a transient Windows AV/indexer
    PermissionError, or corrupt JSON). A mutating op reading a degraded set and writing it
    back would silently discard every user entry - so save()/delete()/set_bool()/put() must
    REFUSE (raise), while read-only accessors stay tolerant.
    """
    import builtins
    import contextlib

    from store import _doc_io, app_settings, prompt_library, schema_cache

    real_open = builtins.open

    @contextlib.contextmanager
    def deny(path: Path):
        """Make exactly `path` raise PermissionError on open (the AV/indexer lock)."""
        def guard(file, *a, **k):
            if isinstance(file, (str, Path)) and Path(file) == Path(path):
                raise PermissionError(13, "locked by another process")
            return real_open(file, *a, **k)
        builtins.open = guard
        try:
            yield
        finally:
            builtins.open = real_open

    # --- read_doc's three-way contract, directly ---
    tmp = Path(tempfile.mkdtemp())
    absent = tmp / "nope.json"
    assert _doc_io.read_doc(absent) is None                      # absent -> None
    good = tmp / "good.json"
    good.write_text('{"k": 1}', encoding="utf-8")
    assert _doc_io.read_doc(good) == {"k": 1}                    # readable -> parsed dict
    bad = tmp / "bad.json"
    bad.write_text("{ not json", encoding="utf-8")
    try:
        _doc_io.read_doc(bad); assert False, "corrupt file must raise"
    except _doc_io.UnreadableStoreError:
        pass
    empty = tmp / "empty.json"
    empty.write_text("", encoding="utf-8")   # 0-byte = interrupted write: unreadable, NOT absent
    try:
        _doc_io.read_doc(empty); assert False, "empty (0-byte) file must raise, not reseed"
    except _doc_io.UnreadableStoreError:
        pass
    with deny(good):                                             # present but locked -> raise
        try:
            _doc_io.read_doc(good); assert False, "locked file must raise"
        except _doc_io.UnreadableStoreError:
            pass

    # --- prompt_library: absence -> seeds; save/delete refuse to clobber a locked file ---
    saved_pt = paths.PROMPT_TEMPLATES
    paths.PROMPT_TEMPLATES = tmp / "prompt_templates.json"
    try:
        # ABSENT: seeds present, and a save persists seeds + the new entry (no loss).
        assert not paths.PROMPT_TEMPLATES.exists()
        seed_names = {t["name"] for t in prompt_library.all_templates()}
        assert seed_names, "seeds must be non-empty when the file is absent"
        prompt_library.save("UserProbe", "POS", "NEG")
        after = {t["name"] for t in prompt_library.all_templates()}
        assert "UserProbe" in after and seed_names <= after, "save dropped the seeds/user entry"
        n_on_disk = len(prompt_library.all_templates())

        # PRESENT-BUT-UNREADABLE: save() and delete() must raise, NOT clobber.
        with deny(paths.PROMPT_TEMPLATES):
            try:
                prompt_library.save("Would-Clobber", "X", "Y")
                assert False, "save() clobbered an unreadable file"
            except _doc_io.UnreadableStoreError:
                pass
            try:
                prompt_library.delete("UserProbe")
                assert False, "delete() clobbered an unreadable file"
            except _doc_io.UnreadableStoreError:
                pass
        # The file is intact - the failed mutation wrote nothing.
        assert len(prompt_library.all_templates()) == n_on_disk
        assert "UserProbe" in {t["name"] for t in prompt_library.all_templates()}
    finally:
        paths.PROMPT_TEMPLATES = saved_pt

    # --- app_settings: set_bool refuses a locked file, get_bool tolerates it ---
    saved_as = paths.APP_SETTINGS
    paths.APP_SETTINGS = tmp / "app_settings.json"
    try:
        app_settings.set_bool("alpha", True)
        app_settings.set_bool("beta", True)
        with deny(paths.APP_SETTINGS):
            # get_bool tolerates (falls back to registered/explicit default), writes nothing.
            assert app_settings.get_bool("alpha", False) is False
            try:
                app_settings.set_bool("gamma", True)
                assert False, "set_bool() clobbered an unreadable file"
            except _doc_io.UnreadableStoreError:
                pass
        # Both original keys survive; the refused write left "gamma" off disk.
        assert app_settings.get_bool("alpha") is True
        assert app_settings.get_bool("beta") is True
        assert app_settings.get_bool("gamma", False) is False
    finally:
        paths.APP_SETTINGS = saved_as

    # --- schema_cache: put refuses a locked file, readers tolerate it ---
    saved_sc = paths.SCHEMA_CACHE
    paths.SCHEMA_CACHE = tmp / "schema_cache.json"
    try:
        schema_cache.put("model/a", {"p": {}})
        schema_cache.put("model/b", {"q": {}})
        with deny(paths.SCHEMA_CACHE):
            assert schema_cache.all_entries() == {}          # tolerant read, no write
            try:
                schema_cache.put("model/c", {"r": {}})
                assert False, "put() clobbered an unreadable file"
            except _doc_io.UnreadableStoreError:
                pass
        assert set(schema_cache.all_entries()) == {"model/a", "model/b"}  # c never landed
    finally:
        paths.SCHEMA_CACHE = saved_sc

    # --- 0-byte store file (interrupted write): a mutating op refuses, it does NOT reseed ---
    saved_pt = paths.PROMPT_TEMPLATES
    paths.PROMPT_TEMPLATES = tmp / "truncated_templates.json"
    try:
        paths.PROMPT_TEMPLATES.write_text("", encoding="utf-8")
        try:
            prompt_library.save("OnEmpty", "X", "Y")
            assert False, "save() reseeded over a 0-byte file"
        except _doc_io.UnreadableStoreError:
            pass
        assert paths.PROMPT_TEMPLATES.read_text(encoding="utf-8") == ""  # untouched
    finally:
        paths.PROMPT_TEMPLATES = saved_pt

    print("store absent-vs-unreadable OK: absent->seeds/defaults; locked/corrupt/empty->"
          "mutations refuse (raise), readers tolerate; no clobber")


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

    # M8: input images are now named by content hash (kp_<sha1>.ext), not source name, so
    # distinct framings never contest the same ComfyUI/input slot. Two different images -> two
    # different names; both copied into the input dir.
    import hashlib as _hl
    name_a = f"kp_{_hl.sha1(a.read_bytes()).hexdigest()}.png"
    name_b = f"kp_{_hl.sha1(b.read_bytes()).hexdigest()}.png"
    assert name_a != name_b

    wf = comfy_client.prepare_workflow(
        template, start_img=str(a), end_img=str(b), prompt="POS", negative="NEG",
        seed=42, node_roles=roles, sets={"12.steps": "30"})
    assert wf["9"]["inputs"]["image"] == name_a
    assert wf["10"]["inputs"]["image"] == name_b
    assert (comfy_client.COMFY_INPUT_DIR / name_a).exists()
    assert (comfy_client.COMFY_INPUT_DIR / name_b).exists()
    assert wf["7"]["inputs"]["text"] == "POS"
    assert wf["8"]["inputs"]["text"] == "NEG"
    assert wf["12"]["inputs"]["noise_seed"] == 42 and wf["13"]["inputs"]["noise_seed"] == 42
    assert wf["12"]["inputs"]["steps"] == 30  # --set int-coerced

    # heuristic fallback (no roles): same result via ascending node-id ordering
    wf2 = comfy_client.prepare_workflow(
        template, start_img=str(a), end_img=str(b), prompt="P2", negative="N2", seed=9)
    assert wf2["9"]["inputs"]["image"] == name_a and wf2["10"]["inputs"]["image"] == name_b
    assert wf2["7"]["inputs"]["text"] == "P2" and wf2["8"]["inputs"]["text"] == "N2"
    assert wf2["12"]["inputs"]["noise_seed"] == 9

    # no end frame -> open-ended: the end-image node (10) is left with no consumers,
    # so the Wan first-last node runs like I2V instead of reusing the baked end frame.
    wf3 = comfy_client.prepare_workflow(
        template, start_img=str(a), end_img=None, prompt="P", negative="N",
        seed=1, node_roles=roles)
    assert wf3["9"]["inputs"]["image"] == name_a            # start still applied
    assert not any(isinstance(v, list) and v and str(v[0]) == "10"
                   for n in wf3.values() for v in n.get("inputs", {}).values()), \
        "end-image node should have no consumers when no end frame is given"

    # A single-LoadImage FLF template whose end image comes from a NON-LoadImage node:
    # node 3 (WanFirstLastFrameToVideo) is fed end_image from node 2, and there is only
    # one LoadImage (node 1, the start). With no declared end_image role an open-ended
    # render can't pin the end node, so it must fail loudly rather than silently reuse the
    # baked end conditioning (the old len(loads) > 1 gate left it connected).
    flf_one_load = {
        "1": {"class_type": "LoadImage", "inputs": {"image": "start.png"}},
        "2": {"class_type": "ImageScale", "inputs": {"image": "start.png"}},
        "3": {"class_type": "WanFirstLastFrameToVideo",
              "inputs": {"start_image": ["1", 0], "end_image": ["2", 0]}},
    }
    try:
        comfy_client.prepare_workflow(copy.deepcopy(flf_one_load), end_img=None)
    except comfy_client.ComfyError:
        pass
    else:
        raise AssertionError("single-LoadImage FLF + no role + no end frame must raise")

    # Regression: a declared end_image role severs even when len(loads) == 1 — the role,
    # not the LoadImage count, drives the disconnect.
    wf_role = comfy_client.prepare_workflow(
        copy.deepcopy(flf_one_load), end_img=None, node_roles={"end_image": "2"})
    assert "end_image" not in wf_role["3"]["inputs"], \
        "declared end_image role must sever the end conditioning with a single LoadImage"
    assert wf_role["3"]["inputs"]["start_image"] == ["1", 0], "start link must survive"

    # A genuine I2V template (single LoadImage, no end-image conditioning at all) must
    # still no-op on a no-end-frame render — not raise. Guards against _has_end_image_-
    # conditioning becoming over-broad and breaking the common open-ended path.
    i2v_one_load = {
        "1": {"class_type": "LoadImage", "inputs": {"image": "start.png"}},
        "2": {"class_type": "WanImageToVideo", "inputs": {"start_image": ["1", 0]}},
    }
    wf_i2v = comfy_client.prepare_workflow(copy.deepcopy(i2v_one_load), end_img=None)
    assert wf_i2v["2"]["inputs"]["start_image"] == ["1", 0], \
        "genuine I2V template must be left intact when no end frame is given"

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
    print("comfy prepare_workflow OK: node-role map + heuristic fallback + --set + open-ended sever "
          "(role-driven; single-load FLF raises; declared role severs at len(loads)==1) + cpu-text-enc")


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


def test_comfy_gpu_cache() -> None:
    # videogen's CUDA kernel cache is isolated into data/gpu_cache + capped (not the global
    # ComputeCache), and wipeable in one call. Redirect GPU_CACHE_DIR to a tempdir so the test
    # never touches the real cache.
    saved_dir = comfy_client.GPU_CACHE_DIR
    saved_path = os.environ.pop("CUDA_CACHE_PATH", None)
    saved_max = os.environ.pop("CUDA_CACHE_MAXSIZE", None)  # clean baseline, then test override
    tmp = Path(tempfile.mkdtemp(prefix="animgen_gpucache_"))
    comfy_client.GPU_CACHE_DIR = tmp
    try:
        env = comfy_client.launch_env()
        assert env["CUDA_CACHE_PATH"] == str(tmp), "launch redirects the CUDA kernel cache"
        assert env["CUDA_CACHE_MAXSIZE"] == str(comfy_client.GPU_CACHE_MAXSIZE_BYTES), "and caps it"
        # a seeded fake kernel is measured then removed; reporting is well-shaped
        (tmp / "kernel.bin").write_bytes(b"x" * 2048)
        assert comfy_client.gpu_cache_size_mb() > 0
        res = comfy_client.clear_gpu_cache()
        assert res == {"files": 1, "bytes": 2048}
        assert comfy_client.gpu_cache_size_mb() == 0
        # clearing an absent cache is non-raising and reports nothing removed
        shutil.rmtree(tmp, ignore_errors=True)
        assert comfy_client.clear_gpu_cache() == {"files": 0, "bytes": 0}
        # an explicit CUDA_CACHE_PATH in the environment is respected (setdefault, not clobbered)
        os.environ["CUDA_CACHE_PATH"] = "X:/custom/cache"
        assert comfy_client.launch_env()["CUDA_CACHE_PATH"] == "X:/custom/cache"
    finally:
        comfy_client.GPU_CACHE_DIR = saved_dir
        for k, v in (("CUDA_CACHE_PATH", saved_path), ("CUDA_CACHE_MAXSIZE", saved_max)):
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(tmp, ignore_errors=True)
    print("comfy GPU cache OK: isolated CUDA_CACHE_PATH + cap + best-effort clear")


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


def test_comfy_api_errors() -> None:
    # M3: _api must distinguish an HTTP error (a real 4xx/5xx response body, e.g. a /prompt
    # validation failure with node_errors) from an unreachable server, surface a truncated/
    # malformed body cleanly, and wrap a read timeout - none of which used to reach a ComfyError
    # with useful text. Drive each via a fake urlopen so no server is touched.
    import io
    import urllib.error
    import urllib.request

    saved = urllib.request.urlopen

    def make_http_error(body: bytes, code: int = 400):
        def urlopen(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, code, "Bad Request", {},
                                         io.BytesIO(body))
        return urlopen

    try:
        # a 400 carrying node_errors -> the node-level detail is surfaced, not "unreachable"
        body = json.dumps({"error": {"message": "invalid prompt"},
                           "node_errors": {"12": {"errors": ["missing model"]}}}).encode()
        urllib.request.urlopen = make_http_error(body)
        try:
            comfy_client._api("/prompt", {"prompt": {}})
            assert False, "expected ComfyError on HTTP 400"
        except comfy_client.ComfyError as e:
            msg = str(e)
            assert "HTTP 400" in msg and "invalid prompt" in msg and "node_errors" in msg, msg
            assert "unreachable" not in msg, "HTTP error must NOT be reported as unreachable"

        # a non-JSON error body -> falls back to the raw text, still a ComfyError (not a crash)
        urllib.request.urlopen = make_http_error(b"<html>500 boom</html>", code=500)
        try:
            comfy_client._api("/history/x")
            assert False, "expected ComfyError on HTTP 500"
        except comfy_client.ComfyError as e:
            assert "HTTP 500" in str(e) and "boom" in str(e)

        # a truncated/malformed 200 body -> JSONDecodeError wrapped as ComfyError (used to escape)
        class FakeResp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return b"{not json"
        urllib.request.urlopen = lambda req, timeout=None: FakeResp()
        try:
            comfy_client._api("/history/x")
            assert False, "expected ComfyError on malformed body"
        except comfy_client.ComfyError as e:
            assert "malformed" in str(e)

        # a read timeout mid-body -> wrapped as ComfyError (socket.timeout is not a URLError)
        import socket
        def urlopen_timeout(req, timeout=None):
            raise socket.timeout("timed out")
        urllib.request.urlopen = urlopen_timeout
        try:
            comfy_client._api("/history/x", timeout=3)
            assert False, "expected ComfyError on read timeout"
        except comfy_client.ComfyError as e:
            assert "timed out" in str(e)
    finally:
        urllib.request.urlopen = saved
    print("comfy _api errors OK: HTTP body surfaced, malformed/timeout wrapped, not 'unreachable'")


def test_poll_retry() -> None:
    # M4: a transient /history poll failure is retried with backoff, not fatal. Stub _api to
    # fail a few times then succeed; the render must still complete. A sustained outage past
    # the streak limit is still fatal. Patch time.sleep so the test doesn't actually wait.
    saved_api, saved_sleep = comfy_client._api, comfy_client.time.sleep
    out = Path(tempfile.mkdtemp()) / "take.mp4"
    src_dir = Path(tempfile.mkdtemp())
    (src_dir / "src.mp4").write_bytes(b"video-bytes")
    hist_ok = {"P": {"status": {"status_str": "success", "completed": True},
                     "outputs": {"9": {"gifs": [{"filename": "src.mp4",
                                                 "subfolder": ""}]}}}}
    # point COMFY_OUTPUT_DIR at our temp dir so the claimed file resolves to our src
    saved_out_dir = comfy_client.COMFY_OUTPUT_DIR
    comfy_client.COMFY_OUTPUT_DIR = src_dir
    try:
        comfy_client.time.sleep = lambda *a, **k: None
        calls = {"n": 0}

        def flaky(path, data=None, timeout=30):
            calls["n"] += 1
            if calls["n"] <= 3:                       # first 3 polls stall
                raise comfy_client.ComfyError("stalled")
            return hist_ok

        comfy_client._api = flaky
        res = comfy_client._poll_until_done("P", out, None, timeout_s=3600, poll_s=0)
        assert calls["n"] == 4, f"should have retried 3 stalls then succeeded: {calls['n']}"
        assert Path(res["video_path"]).exists()

        # a sustained outage (always fails) trips the streak limit and raises
        calls["n"] = 0
        comfy_client._api = lambda *a, **k: (_ for _ in ()).throw(comfy_client.ComfyError("down"))
        try:
            comfy_client._poll_until_done("P", out, None, timeout_s=3600, poll_s=0)
            assert False, "a sustained poll outage must eventually raise"
        except comfy_client.ComfyError as e:
            assert "lost contact" in str(e)

        # the failure branch still honors the overall timeout (accounting for the upcoming
        # backoff sleep): with the budget exhausted, a stalling poll raises "timed out"
        # instead of retrying - the streak limit must not be the only bound.
        try:
            comfy_client._poll_until_done("P", out, None, timeout_s=0, poll_s=0)
            assert False, "an exhausted timeout on the failure branch must raise"
        except comfy_client.ComfyError as e:
            assert "timed out" in str(e)
    finally:
        comfy_client._api, comfy_client.time.sleep = saved_api, saved_sleep
        comfy_client.COMFY_OUTPUT_DIR = saved_out_dir
    print("comfy poll retry OK: transient stalls retried, sustained outage fatal")


def test_claim_output_empty() -> None:
    # L10: a /history entry whose outputs carry only unrecognized keys yields an empty produced
    # list; claiming it used to return a DONE take pointing at a never-written file. Now it raises.
    out = Path(tempfile.mkdtemp()) / "take.mp4"
    entry = {"outputs": {"9": {"latents": [{"filename": "x.latent", "subfolder": ""}]}}}
    assert comfy_client._entry_outputs(entry) == [], "unrecognized keys produce nothing"
    try:
        comfy_client._claim_output([], out)
        assert False, "empty produced list must raise, not claim a nonexistent file"
    except comfy_client.ComfyError as e:
        assert "recognized media" in str(e)
    assert not out.exists(), "no file should have been claimed"
    print("comfy claim empty OK: no-media output raises instead of a phantom DONE take")


def test_stop_work_targets_prompt() -> None:
    # L8: stop_work targets our own prompt id (delete: [id]) instead of clearing the whole
    # server queue; only falls back to clear:true when no id is known. Capture the POST bodies.
    saved_post = comfy_client._post
    posts = []
    comfy_client._post = lambda path, data=None, timeout=5: posts.append((path, data))
    try:
        comfy_client.stop_work(prompt_id="P7")
        assert ("/queue", {"delete": ["P7"]}) in posts, posts
        assert ("/interrupt", {}) in posts
        assert not any(d == {"clear": True} for _, d in posts), "must NOT clear the whole queue"

        posts.clear()
        comfy_client.stop_work()   # no id -> blanket clear fallback preserved
        assert ("/queue", {"clear": True}) in posts, posts
    finally:
        comfy_client._post = saved_post
    print("comfy stop_work OK: targets our prompt id, falls back to clear when unknown")


def test_total_price() -> None:
    from ui.cost_confirm import total_price_text

    # Hosted estimates sum; local $0 adds nothing; None tallies as unknown.
    assert total_price_text([0.72, 0.0, 1.28]) == "Full set: $2.00"
    assert total_price_text([0.72, None, 0.0]) == "Full set: $0.72  (+1 unknown)"
    assert total_price_text([]) == "Full set: $0.00"
    assert total_price_text([None, None]) == "Full set: $0.00  (+2 unknown)"
    # L13 sibling surface: a sub-cent full set doesn't collapse to $0.00 either.
    assert total_price_text([0.004]) == "Full set: $0.004"
    assert total_price_text([0.0004]) == "Full set: <$0.01"
    print("cost_confirm total_price_text OK: sum, free, unknown tally, empty, sub-cent")


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


def test_launch_label() -> None:
    from ui.cost_confirm import build_summary, launch_button_label

    # All-unknown-cost batch: build_summary -> total=0, has_spend=True. The launch
    # button must NOT read "free" (rule #1: the gate must not contradict its own
    # "MAY spend money" header).
    unknown_items = [
        {"name": "a", "model_display": "Mystery v1", "est_cost": None, "params": {}},
        {"name": "b", "model_display": "Mystery v2", "est_cost": None, "params": {}},
    ]
    _, total_u, has_spend_u = build_summary(unknown_items)
    assert total_u == 0.0 and has_spend_u is True
    label_u = launch_button_label(total_u, has_spend_u)
    assert label_u != "Launch (spend ~free)"
    assert label_u == "Launch (cost unknown)"

    # No-spend (local $0) batch keeps "Launch (free)".
    assert launch_button_label(0.0, False) == "Launch (free)"

    # Known hosted cost keeps the dollar amount.
    assert launch_button_label(2.0, True) == "Launch (spend ~$2.00)"

    # Mixed known + unknown (total>0, has_spend) still shows the known total.
    mixed_items = [
        {"name": "a", "model_display": "Seedance", "est_cost": 0.72, "params": {}},
        {"name": "b", "model_display": "Mystery", "est_cost": None, "params": {}},
    ]
    _, total_m, has_spend_m = build_summary(mixed_items)
    assert launch_button_label(total_m, has_spend_m) == "Launch (spend ~$0.72)"
    print("cost_confirm launch_button_label OK: unknown-not-free, free, known, mixed")


def test_subcent_cost_display() -> None:
    """L13: sub-cent totals must NOT collapse to '$0.00' next to a 'spend real money'
    warning. _fmt_cost shows sub-cent precision (or '<$0.01'), and the launch label
    inherits it so the gate can't read '$0.00 spend'."""
    from ui.cost_confirm import _fmt_cost, launch_button_label, build_summary

    assert _fmt_cost(None) == "?"
    assert _fmt_cost(0) == "free"          # exactly free stays "free", not "<$0.01"
    assert _fmt_cost(0.0004) == "<$0.01"   # below the 3-decimal floor
    assert _fmt_cost(0.004) == "$0.004"    # sub-cent but representable to 3 decimals
    assert _fmt_cost(0.009) == "$0.009"
    assert _fmt_cost(0.01) == "$0.01"      # cent boundary keeps 2-decimal form
    assert _fmt_cost(0.72) == "$0.72"
    assert _fmt_cost(2.0) == "$2.00"

    # A batch whose total is sub-cent no longer renders "$0.00" in header or launch label.
    body, total, has_spend = build_summary(
        [{"name": "tiny", "model_display": "Cheap v1", "est_cost": 0.004, "params": {}}])
    assert "$0.00 " not in body and "$0.004" in body, body
    label = launch_button_label(total, has_spend)
    assert label == "Launch (spend ~$0.004)", label
    print("cost_confirm sub-cent OK: <$0.01 / $0.00X, no $0.00 spend")


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


def test_recovered_crash_maps_to_interrupted() -> None:
    # A render error re-raised by crash recovery after a successful final restart carries the
    # CRASH_INTERRUPTED_ATTR stamp; the worker must record the take FAILED + interrupted=True so
    # the bulk "Restart interrupted takes" action picks it up (card #68). The QueueAbandoned raised
    # when the queue is abandoned (final restart failed / server stayed down) is ALSO stamped, so
    # the crash VICTIM lands FAILED + interrupted=True like its abandon_local'd siblings, not
    # interrupted=False (card #71). A plain (unstamped) workflow error must still land FAILED +
    # interrupted=False. Run the QRunnable directly.
    from PySide6.QtWidgets import QApplication

    from backends.crash_recovery import CRASH_INTERRUPTED_ATTR, _abandon
    from backends.jobs import GenerationJob, JobManager
    from store.models import STATUS_FAILED, STATUS_PENDING

    app = QApplication.instance() or QApplication([])
    project = Project.new()
    shot = project.add_shot("kick", model_id="local-flf-wan14b")
    jm = JobManager(project)

    crashed = project.add_take(shot.id, status=STATUS_PENDING)

    def stamped_runner(progress):
        e = RuntimeError("ComfyUI unreachable (TDR)")     # the original render error,
        setattr(e, CRASH_INTERRUPTED_ATTR, True)          # stamped by the recover path
        raise e

    GenerationJob(project, crashed.id, "comfyui", stamped_runner, jm._signals,
                  jm._cancelled, jm._stopping, jm._requeue, jm._on_job_done).run()

    plain = project.add_take(shot.id, status=STATUS_PENDING)

    def plain_runner(progress):
        raise RuntimeError("bad node")                    # genuine workflow error, no stamp

    GenerationJob(project, plain.id, "comfyui", plain_runner, jm._signals,
                  jm._cancelled, jm._stopping, jm._requeue, jm._on_job_done).run()

    abandoned = project.add_take(shot.id, status=STATUS_PENDING)

    def abandoned_runner(progress):
        # The real abandon path raises a QueueAbandoned built (and stamped) by _abandon; the
        # crash victim in the worker must land FAILED + interrupted=True too (card #71).
        raise _abandon(lambda _r: None, lambda _r: None,
                       "ComfyUI still unreachable after a final restart; pausing the local queue.")

    GenerationJob(project, abandoned.id, "comfyui", abandoned_runner, jm._signals,
                  jm._cancelled, jm._stopping, jm._requeue, jm._on_job_done).run()
    # The take state below is read straight from the write-through (project.update_take runs
    # inline in run()); processEvents only drains the emitted signals, which this test ignores.
    app.processEvents()

    got_crash = project.get_take(crashed.id)
    assert got_crash.status == STATUS_FAILED and got_crash.interrupted is True, (
        got_crash.status, got_crash.interrupted)
    assert "TDR" in (got_crash.error or ""), "must keep the original error verbatim"
    got_plain = project.get_take(plain.id)
    assert got_plain.status == STATUS_FAILED and got_plain.interrupted is False, (
        got_plain.status, got_plain.interrupted)
    got_abandoned = project.get_take(abandoned.id)
    assert got_abandoned.status == STATUS_FAILED and got_abandoned.interrupted is True, (
        got_abandoned.status, got_abandoned.interrupted)
    assert "pausing the local queue" in (got_abandoned.error or ""), got_abandoned.error
    print("recovered crash OK: stamped re-raise + abandon victim -> FAILED+interrupted; "
          "plain error -> FAILED only")


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
        # L8: request_stop targets the take's own prompt id (None here - not yet recorded)
        assert lt.id in jm._stopping and ("comfy", (), {"prompt_id": None}) in calls

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


def test_sampler_step_plan() -> None:
    from backends.comfy_client import sampler_step_plan, progress_fraction as pf

    # Wan 2.2 two-expert split: node 12 does steps 0..10, node 13 does 10..20 (steps=20 each).
    wf = {"12": {"class_type": "KSamplerAdvanced",
                 "inputs": {"steps": 20, "start_at_step": 0, "end_at_step": 10}},
          "13": {"class_type": "KSamplerAdvanced",
                 "inputs": {"steps": 20, "start_at_step": 10, "end_at_step": 10000}},
          "9": {"class_type": "SaveImage", "inputs": {}}}
    plan = sampler_step_plan(wf)
    assert plan == {"12": (0, 20), "13": (10, 20)}, plan

    # stage 1 mid-run -> continuous count, not 0..10
    assert pf({"type": "progress", "data": {"value": 5, "max": 10, "node": "12", "prompt_id": "p1"}},
              "p1", plan) == (0.25, "step 5/20")
    # stage 2 mid-run -> picks up where stage 1 left off (no restart to 0)
    assert pf({"type": "progress_state",
               "data": {"prompt_id": "p1",
                        "nodes": {"12": {"value": 10, "max": 10},        # finished
                                  "13": {"value": 4, "max": 10}}}},      # running
              "p1", plan) == (0.7, "step 14/20")

    # single-sampler workflow -> empty plan -> raw per-node behaviour preserved
    assert sampler_step_plan({"3": {"class_type": "KSampler", "inputs": {"steps": 25}}}) == {}
    # no plan / node not in plan -> unchanged value/max
    assert pf({"type": "progress", "data": {"value": 5, "max": 10, "node": "99"}}, "p1", plan) \
        == (0.5, "step 5/10")
    print("sampler_step_plan OK: 2-expert chain maps to one continuous 0..total step count")


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

    # Minimal stand-in for a Project's take population, so ambiguous_seeds() can be exercised
    # with hand-built Take lists (it only calls .list_takes(include_deleted=...)).
    class _FakeProj:
        def __init__(self, takes):
            self._takes = takes

        def list_takes(self, shot_id=None, *, include_deleted=False):
            return list(self._takes)

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
    # A take binned while still mid-flight IS reconciled (H2) - otherwise it sits `generating`
    # forever and its ComfyUI render keeps running unclaimed. It's the newest of the generating
    # group (added last), but `created` only has second resolution, so keep the intra-group
    # assertion order-agnostic.
    binned = project.add_take(shot.id, status=STATUS_GENERATING, seed=300, deleted=True,
                              settings_snapshot=snap_local)
    orphans = recovery.comfy_orphans(project)
    assert len(orphans) == 3, [o.id for o in orphans]
    assert {o.id for o in orphans} == {gen.id, binned.id, pend.id}, "hosted/done excluded, binned in"
    assert orphans[-1].id == pend.id, "generating-first, pending last"

    # plan_comfy_recovery: the four actions + prompt-id match + seed match + claim dedup.
    def t(tid, status, seed=None, job=None, shot="s"):
        return Take(id=tid, shot_id=shot, status=status, seed=seed, backend_job_id=job)

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

    # M5 - fixed-seed shot: a shot with an AUTHORED seed produces N takes sharing one seed, so a
    # bare seed match is ambiguous. A never-submitted PENDING orphan (no backend_job_id) must NOT
    # misclaim a SIBLING's finished /history entry as its own; it's left CANCEL (interrupted,
    # restartable). Same guard blocks REATTACHing to a sibling's live /queue entry.
    fs_hist = [{"prompt_id": "Pfs", "seeds": {77}, "outputs": [Path("out/sib.mp4")], "ok": True}]
    fs_queue = [{"prompt_id": "Qfs", "seeds": {77}, "state": "running"}]
    # shot "fixed" has two takes on seed 77 (fixed-seed); one is a mid-flight orphan, but the
    # OTHER sibling is a non-orphan DONE take whose render "Pfs" that history belongs to.
    ambig = recovery.ambiguous_seeds(_FakeProj([
        t("fs_orphan", STATUS_PENDING, seed=77, shot="fixed"),
        t("fs_done",   STATUS_DONE,    seed=77, shot="fixed"),   # sibling, owns Pfs
    ]))
    assert ("fixed", 77) in ambig, "shared seed within a shot is flagged ambiguous"
    fs_orphans = [t("fs_orphan", STATUS_PENDING, seed=77, shot="fixed")]
    fs_plans = {p.take_id: p for p in
                recovery.plan_comfy_recovery(fs_orphans, fs_hist, fs_queue, ambig)}
    assert fs_plans["fs_orphan"].action == recovery.CANCEL, \
        "fixed-seed PENDING orphan must NOT RECLAIM/REATTACH a sibling's render"
    assert fs_plans["fs_orphan"].output_path is None, "no wrong video is claimed"
    assert "fixed-seed" in fs_plans["fs_orphan"].reason, "reason explains the ambiguous-seed skip"

    # A GENERATING fixed-seed orphan is likewise not misattributed -> FAIL (interrupted).
    fs_gen = [t("fs_gen", STATUS_GENERATING, seed=77, shot="fixed")]
    fs_gen_plan = recovery.plan_comfy_recovery(fs_gen, fs_hist, fs_queue, ambig)[0]
    assert fs_gen_plan.action == recovery.FAIL and fs_gen_plan.prompt_id is None

    # But a fixed-seed orphan that DID record its own backend_job_id still matches by prompt-id
    # (that's unambiguous) - the guard only refuses the SEED fallback, not prompt-id matching.
    fs_byid = [t("fs_byid", STATUS_GENERATING, seed=77, job="Pfs", shot="fixed")]
    assert recovery.plan_comfy_recovery(fs_byid, fs_hist, fs_queue, ambig)[0].action == recovery.RECLAIM, \
        "prompt-id match is authoritative even for a fixed-seed shot"

    # Regression guard: a UNIQUE seed (random-seed shot, one take per seed) still matches - the
    # guard must not over-fire and refuse legitimate single-take seed matching.
    uniq = recovery.ambiguous_seeds(_FakeProj([t("solo", STATUS_PENDING, seed=100, shot="s")]))
    assert ("s", 100) not in uniq, "a seed carried by exactly one take is not ambiguous"
    solo_plan = recovery.plan_comfy_recovery(
        [t("solo", STATUS_GENERATING, seed=100)], history, queue, uniq)[0]
    assert solo_plan.action == recovery.RECLAIM and solo_plan.prompt_id == "Pdone", \
        "unique-seed matching still works with the ambiguity guard in place"

    # plan_offline_recovery: ComfyUI unreachable (no history/queue). Nothing can be verified
    # and no worker is live, so every orphan is cleared rather than left a permanent "running"
    # zombie: any generating take (submitted or not) -> FAIL; a pending take -> CANCEL.
    offline = [
        t("submitted",  STATUS_GENERATING, seed=10, job="Plive"),  # had prompt id -> FAIL
        t("zombie",     STATUS_GENERATING, seed=20),               # no prompt id  -> FAIL
        t("queued",     STATUS_PENDING,    seed=30),               # never ran     -> CANCEL
    ]
    off = {p.take_id: p for p in recovery.plan_offline_recovery(offline)}
    assert off["submitted"].action == recovery.FAIL, "submitted-but-unverifiable take must fail, not linger"
    assert off["zombie"].action == recovery.FAIL, "generating-without-prompt-id must not be left"
    assert off["queued"].action == recovery.CANCEL
    print("orphan recovery OK: select + reclaim/reattach/fail/cancel + prompt-id + seed dedup "
          "+ fixed-seed ambiguity guard (M5) + offline fail/cancel")


def test_crash_recovery() -> None:
    from backends.crash_recovery import (CRASH_INTERRUPTED_ATTR, QueueAbandoned, _looks_crashed,
                                         format_elapsed, run_with_crash_recovery)

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

    # (c) crash every time AND the server stays down through the final restart -> genuinely
    # dead: QueueAbandoned, on_abandon once. Now 3 restarts, not 2: the last strike also tries
    # one final restart before giving up (it just doesn't bring the server back here), so the
    # abandon decision rests on the server's state, not the raw attempt count.
    notes, restarts, abandons = [], [], []
    def always_crash():
        raise comfy_client.ComfyError("ComfyUI unreachable")
    try:
        run_with_crash_recovery(
            render=always_crash, server_running=lambda: False,
            restart_server=lambda: restarts.append(1),
            note=notes.append, on_abandon=abandons.append, clock=make_clock())
        assert False, "expected QueueAbandoned"
    except QueueAbandoned as e:
        # The crash VICTIM (this take) is stamped interrupted, like its abandon_local'd
        # siblings - it crashed 3x, so the bulk Restart must pick it up too (card #71).
        assert getattr(e, CRASH_INTERRUPTED_ATTR, False) is True, "abandon victim must be stamped"
    assert len(restarts) == 3 and len(abandons) == 1, (restarts, abandons)
    assert "crashed 3x" in abandons[0] and "pausing the local queue" in abandons[0]
    assert "unreachable after a final restart" in abandons[0], abandons[0]

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
    except QueueAbandoned as e:
        assert getattr(e, CRASH_INTERRUPTED_ATTR, False) is True, "abandon victim must be stamped"
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

    # (h) crash on every attempt, but the FINAL restart brings ComfyUI back responsive: a third
    # transient TDR must NOT pause the whole local queue on attempt count alone. The render
    # models a GPU watchdog crash that takes the server down; restart_server brings it back, so
    # server_running reads "down" at each crash detection but "up" at the post-final-restart
    # probe. Expect a restart on the last strike (3 total), no abandon, and the render error
    # re-raised so only THIS take fails while the rest of the queue keeps rendering.
    notes, restarts, abandons = [], [], []
    server_up = [True]
    def render_tdr():
        server_up[0] = False               # the crash (TDR) takes ComfyUI down
        raise comfy_client.ComfyError("ComfyUI unreachable (TDR)")
    def restart_recover():
        restarts.append(1)
        server_up[0] = True                # the restart brings the server back up
    try:
        run_with_crash_recovery(
            render=render_tdr, server_running=lambda: server_up[0],
            restart_server=restart_recover,
            note=notes.append, on_abandon=abandons.append, clock=make_clock())
        assert False, "expected the render error to propagate after recovery"
    except comfy_client.ComfyError as e:
        assert not isinstance(e, QueueAbandoned), "a recovered server must NOT abandon the queue"
        assert "(TDR)" in str(e), ("must re-raise the original render error verbatim", str(e))
        # The re-raised crash error is STAMPED so the worker records the take interrupted=True
        # (bulk-restartable) - it WAS crash-killed, not a genuine workflow error (card #68).
        assert getattr(e, CRASH_INTERRUPTED_ATTR, False) is True, "recovered crash must be stamped"
    assert len(restarts) == 3, ("a final restart must fire on the last strike", restarts)
    assert not abandons, "on_abandon must NOT fire when the final restart recovers the server"
    assert any("recovered after a final restart" in n for n in notes), notes

    # (i) restart succeeds on the first two strikes but RAISES on the final one -> abandon via
    # the final-strike restart-fail path (distinct from (e), which raises on the very first
    # strike). Covers the production-realistic case where comfy_client.restart_server's own
    # block-until-responsive gives up on the last try.
    notes, restarts, abandons = [], [], []
    def restart_fails_on_last():
        restarts.append(1)
        if len(restarts) >= 3:
            raise comfy_client.ComfyError("did not come back up")
    try:
        run_with_crash_recovery(
            render=always_crash, server_running=lambda: False,
            restart_server=restart_fails_on_last,
            note=notes.append, on_abandon=abandons.append, clock=make_clock())
        assert False, "expected QueueAbandoned"
    except QueueAbandoned as e:
        assert getattr(e, CRASH_INTERRUPTED_ATTR, False) is True, "abandon victim must be stamped"
    assert len(restarts) == 3 and len(abandons) == 1, (restarts, abandons)
    assert "restart failed" in abandons[0] and "did not come back up" in abandons[0], abandons[0]

    # (j) L21 (Wave3 G): max_attempts < 1 is caller misuse - the loop would never run and the
    # trailing raw QueueAbandoned would bypass _abandon (no on_abandon, no CRASH_INTERRUPTED_ATTR).
    # The up-front assert rejects it instead, so on_abandon is never left uncalled by a silent
    # fall-through.
    for bad in (0, -1):
        raised = None
        try:
            run_with_crash_recovery(
                render=lambda: {"ok": True}, server_running=lambda: True,
                restart_server=lambda: None, note=lambda _l: None,
                on_abandon=lambda _r: None, clock=make_clock(), max_attempts=bad)
        except AssertionError as e:
            raised = str(e)
        assert raised is not None and "max_attempts" in raised, (bad, raised)

    print("crash_recovery OK: success/retry/abandon/workflow-error/restart-fail/"
          "transient-down/user-abort/final-restart-recovers/final-restart-fails/"
          "max-attempts-guard + format_elapsed")


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

        # L9: a transient taskkill stall in stop_server surfaces as subprocess.TimeoutExpired
        # (NOT a ComfyError), which used to escape restart_server and abandon the whole local
        # queue. It must now be swallowed and the relaunch must still happen.
        order.clear()
        def stop_stall():
            order.append("stop")
            raise subprocess.TimeoutExpired(cmd="taskkill", timeout=10)
        comfy_client.stop_server = stop_stall
        comfy_client.restart_server(settle_s=0)
        assert order == ["stop", "launch"], f"TimeoutExpired must not abort restart: {order}"
        comfy_client.stop_server = stop   # restore the ComfyError-raising stub for the rest

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

    # resume_local skips ids that are no longer PENDING (already done) or unknown (runner
    # dropped) - a no-op that still clears the flag and re-enqueues nothing.
    assert jm.resume_local([q1.id, q2.id, "nonexistent-id"]) == 0
    assert jm.is_local_paused() is False
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
    comfy_client.stop_work = lambda *a, **k: interrupted.set()  # type: ignore[assignment]
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


def test_clear_local_pause() -> None:
    """clear_local_pause lifts the pause flag without re-enqueuing anything (the non-batch
    manual-stop drain path, card #42) - distinct from resume_local which re-runs held takes."""
    from PySide6.QtWidgets import QApplication

    from backends.jobs import JobManager

    QApplication.instance() or QApplication([])
    jm = JobManager(Project.new())
    assert jm.is_local_paused() is False
    jm.pause_local()                     # nothing queued, but the flag flips on
    assert jm.is_local_paused() is True
    jm.clear_local_pause()
    assert jm.is_local_paused() is False  # cleared, and (unlike resume_local) re-enqueued nothing
    print("clear_local_pause OK: pause flag lifted with no re-enqueue")


def test_stop_pauses_nonbatch_local() -> None:
    """A deliberate ComfyUI stop with non-batch local work in flight (card #42): the local
    queue is paused (so crash-recovery won't fight the stop) and the queued local takes are
    cancelled (no Resume UI). Mirrors MainWindow._pause_local_on_stop_intent without Qt wiring:
    pause_local() + cancel_take() per held id, then clear_local_pause() once drained."""
    import threading
    import time

    from PySide6.QtWidgets import QApplication

    from backends.jobs import JobManager
    from store.models import STATUS_CANCELLED, STATUS_FAILED, STATUS_GENERATING, STATUS_PENDING

    app = QApplication.instance() or QApplication([])
    project = Project.new()
    shot = project.add_shot("kick", model_id="local-flf-wan14b")
    jm = JobManager(project)

    local_snap = {"backend": "comfyui"}
    gate = threading.Event()
    active = project.add_take(shot.id, status=STATUS_PENDING, settings_snapshot=local_snap)
    q1 = project.add_take(shot.id, status=STATUS_PENDING, settings_snapshot=local_snap)
    q2 = project.add_take(shot.id, status=STATUS_PENDING, settings_snapshot=local_snap)

    started = {"q": False}
    def active_runner(progress):
        gate.wait(timeout=10)                 # held until the "stop" fires, then fails
        raise RuntimeError("ComfyUI shut down")
    def quick(progress):
        started["q"] = True                   # must never run - these get cancelled
        return {"video_path": "q.mp4"}

    jm.enqueue(active.id, "comfyui", active_runner)
    jm.enqueue(q1.id, "comfyui", quick)
    jm.enqueue(q2.id, "comfyui", quick)

    for _ in range(100):
        if project.get_take(active.id).status == STATUS_GENERATING:
            break
        time.sleep(0.02)
    assert project.get_take(active.id).status == STATUS_GENERATING

    # The stop-intent path: pause the local queue, then cancel the held (queued) local takes.
    held = jm.pause_local()
    assert jm.is_local_paused() is True
    assert set(held) == {q1.id, q2.id}, held
    for tid in held:
        assert jm.cancel_take(tid) is True
    assert project.get_take(q1.id).status == STATUS_CANCELLED
    assert project.get_take(q2.id).status == STATUS_CANCELLED

    # The GENERATING take now fails; while paused it must NOT be requeued/restarted - it just
    # ends terminal (FAILED here, since this stub isn't flagged via request_stop).
    gate.set()
    for _ in range(200):
        if project.get_take(active.id).status == STATUS_FAILED:
            break
        time.sleep(0.02)
    app.processEvents()
    assert project.get_take(active.id).status == STATUS_FAILED, project.get_take(active.id).status

    # Drained: lift the transient pause. The cancelled takes never ran.
    jm.clear_local_pause()
    assert jm.is_local_paused() is False
    time.sleep(0.1)
    assert started["q"] is False, "cancelled queued local takes must never run after a stop"
    print("stop_pauses_nonbatch_local OK: queue paused, queued takes cancelled, no restart")


def test_stop_handler_nonbatch() -> None:
    """Drive the real MainWindow._pause_local_on_stop_intent + _on_status_changed (card #42):
    a manual ComfyUI stop with non-batch local work pauses the local queue, cancels the queued
    takes, and the transient pause is auto-cleared once the in-flight take drains - never left
    stuck True. Covers both the GENERATING case (held until the take fails) and the purely-queued
    case (cleared right away, since no terminal status_changed will arrive to trigger it)."""
    import threading
    import time

    from PySide6.QtWidgets import QApplication

    from store.models import STATUS_CANCELLED, STATUS_FAILED, STATUS_GENERATING, STATUS_PENDING
    from ui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    win = MainWindow(Project.new("Untitled"))
    proj = win.project
    shot = proj.add_shot("kick", model_id="local-flf-wan14b")
    local_snap = {"backend": "comfyui"}

    # GENERATING case: one in-flight take with a queued take behind it.
    gate = threading.Event()
    ran = {"q": False}
    active = proj.add_take(shot.id, status=STATUS_PENDING, settings_snapshot=local_snap)
    q1 = proj.add_take(shot.id, status=STATUS_PENDING, settings_snapshot=local_snap)

    def active_runner(progress):
        gate.wait(timeout=10)
        raise RuntimeError("ComfyUI shut down")     # the manual stop kills the render
    def queued(progress):
        ran["q"] = True                             # must never run - it gets cancelled
        return {"video_path": "q.mp4"}

    win.jobs.enqueue(active.id, "comfyui", active_runner)
    win.jobs.enqueue(q1.id, "comfyui", queued)
    for _ in range(100):
        if proj.get_take(active.id).status == STATUS_GENERATING:
            break
        time.sleep(0.02)
    assert proj.get_take(active.id).status == STATUS_GENERATING

    win._pause_local_on_stop_intent()               # <- the real stop-intent handler
    assert win.jobs.is_local_paused() is True and win._stop_paused_local is True
    assert proj.get_take(q1.id).status == STATUS_CANCELLED   # queued take cancelled (no Resume UI)

    gate.set()                                      # the in-flight take now fails
    for _ in range(200):
        app.processEvents()                         # deliver the worker's queued status_changed
        if proj.get_take(active.id).status == STATUS_FAILED:
            break
        time.sleep(0.02)
    app.processEvents()
    assert proj.get_take(active.id).status == STATUS_FAILED
    assert win._stop_paused_local is False          # drained -> transient pause auto-cleared
    assert win.jobs.is_local_paused() is False
    assert ran["q"] is False, "a cancelled queued take must never run"

    # Purely-queued case: nothing GENERATING, so the handler lifts the pause immediately (no
    # terminal status_changed would otherwise arrive to clear it). Takes are queued in the
    # project but not handed to the pool, modelling the instant before any worker starts.
    p1 = proj.add_take(shot.id, status=STATUS_PENDING, settings_snapshot=local_snap)
    p2 = proj.add_take(shot.id, status=STATUS_PENDING, settings_snapshot=local_snap)
    win._pause_local_on_stop_intent()
    assert proj.get_take(p1.id).status == STATUS_CANCELLED
    assert proj.get_take(p2.id).status == STATUS_CANCELLED
    assert win._stop_paused_local is False and win.jobs.is_local_paused() is False
    print("stop_handler_nonbatch OK: real handler pauses, cancels queued, auto-clears on drain")


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

    # queue_order: round-major (one take of every eligible shot per round, repeated N),
    # NOT shot-major (all N of shot 1 then all N of shot 2). eligible items are
    # (shot, model, settings, est); check the shot-name sequence.
    order = [shot.name for shot, _m, _s, _e in batch.queue_order(plan.eligible, 3)]
    assert order == ["ok1", "ok2", "ok1", "ok2", "ok1", "ok2"], order
    assert [s.name for s, *_ in batch.queue_order(plan.eligible, 1)] == ["ok1", "ok2"]
    # n<1 floors at one round
    floored = batch.queue_order(plan.eligible, 0)
    assert [s.name for s, *_ in floored] == ["ok1", "ok2"], floored
    assert batch.queue_order([], 5) == []                         # empty eligible -> empty

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
    print("batch OK: plan eligibility/N-per-shot, round-major queue_order, "
          "BatchRun completion, report, sleep cmd")


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


def test_restart_plan() -> None:
    """Pure restart.plan_restart: new-format cancelled takes with a known model + present start
    frame are restartable; the rest are reported unrestartable with a reason (caller fails them)."""
    from backends import restart
    from store.models import STATUS_CANCELLED, STATUS_DONE, Take

    def _take(snap, status=STATUS_CANCELLED, shot="s"):
        return Take(id="t" + str(id(snap))[-6:], shot_id=shot, status=status,
                    settings_snapshot=snap)

    framed = {"model_id": "good", "backend": "replicate", "start_frame": "a.png",
              "settings": {"seed": 7, "duration": 4}, "canvas": [1254, 1254], "crop": {}}
    ok = _take(framed)
    ok_with_end = _take({**framed, "end_frame": "b.png"})   # present end frame -> still restartable
    failed_ok = _take(framed, status=STATUS_FAILED)         # interrupted FAILED orphan -> restartable too
    unknown_model = _take({**framed, "model_id": "gone"})
    missing_frame = _take({**framed, "start_frame": "deleted.png"})
    missing_end = _take({**framed, "end_frame": "deleted_end.png"})  # end frame gone -> can't replay exactly
    old_snapshot = _take({"model_id": "good", "backend": "replicate",  # pre-2026-06-17: no canvas/crop
                          "start_frame": "a.png", "settings": {"seed": 1}})
    not_terminal = _take(framed, status=STATUS_DONE)  # not CANCELLED/FAILED -> filtered out entirely

    models = {"good": {"display_name": "Good", "backend": "replicate"}}
    plan = restart.plan_restart(
        [ok, ok_with_end, failed_ok, unknown_model, missing_frame, missing_end, old_snapshot,
         not_terminal],
        model_of_id=lambda mid: models.get(mid),
        est_of=lambda mid, s: 0.5,
        path_exists=lambda p: p in ("a.png", "b.png"),
        name_of=lambda t: t.id)

    assert plan.restartable == [ok, ok_with_end, failed_ok], plan.restartable
    assert len(plan.items) == 3
    it = plan.items[0]
    assert set(it) >= {"name", "model_display", "backend", "est_cost", "params"}
    assert it["est_cost"] == 0.5 and it["params"]["seed"] == 7
    reasons = {t.id: r for t, r in plan.unrestartable}
    assert set(reasons) == {unknown_model.id, missing_frame.id, missing_end.id,
                            old_snapshot.id}, reasons
    assert "unknown model" in reasons[unknown_model.id]
    assert "start keyframe" in reasons[missing_frame.id]
    assert "end keyframe" in reasons[missing_end.id]
    assert "predates framing" in reasons[old_snapshot.id]
    print("restart plan OK: cancelled+failed restartable filter + per-take unrestartable reasons")


def test_restart_take() -> None:
    """JobManager.restart_take clears a cancelled take's stale `_cancelled` membership so the
    re-enqueued worker actually runs it (rather than bailing straight back to CANCELLED)."""
    from PySide6.QtWidgets import QApplication
    from backends.jobs import JobManager
    from store.models import STATUS_CANCELLED, STATUS_DONE, STATUS_PENDING

    app = QApplication.instance() or QApplication([])  # noqa: F841
    project = Project.new()
    shot = project.add_shot("kick", model_id="seedance-2.0-std")
    take = project.add_take(shot.id, status=STATUS_PENDING,
                            settings_snapshot={"backend": "replicate"})
    jm = JobManager(project)
    jm.cancel_take(take.id)
    assert project.get_take(take.id).status == STATUS_CANCELLED
    assert take.id in jm._cancelled

    project.update_take(take.id, status=STATUS_PENDING, error=None)
    jm.restart_take(take.id, "replicate", lambda p: {"video_path": "redo.mp4"})
    assert jm.wait_for_done(20000), "restart job did not finish"
    app.processEvents()
    got = project.get_take(take.id)
    assert got.status == STATUS_DONE and got.video_path == "redo.mp4", (got.status, got.video_path)
    assert take.id not in jm._cancelled
    print("restart_take OK: stale cancel cleared, re-enqueued take runs to done")


def test_restart_from_snapshot() -> None:
    """MainWindow restart: a cancelled take with a full snapshot replays IN PLACE (same id, runner
    fed the snapshot's frozen model/seed/framing); an unrestartable one is marked FAILED with a
    reason; the takes-grid context menu offers Restart for a cancelled take without exec()."""
    import tempfile
    from pathlib import Path

    from PySide6.QtWidgets import QApplication, QMessageBox
    from ui.main_window import MainWindow
    from ui.takes_view import TakesView
    from store.models import STATUS_CANCELLED, STATUS_FAILED, STATUS_PENDING

    app = QApplication.instance() or QApplication([])  # noqa: F841
    orig_info = QMessageBox.information
    QMessageBox.information = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok)
    start_png = Path(tempfile.mkdtemp()) / "a.png"
    start_png.write_bytes(b"not-a-real-png")   # plan_restart only checks the path exists
    try:
        project = Project.new()
        shot = project.add_shot("kick", model_id="seedance-2.0-std")
        snap = {"model_id": "seedance-2.0-std", "backend": "replicate",
                "start_frame": str(start_png), "end_frame": None,
                "settings": {"seed": 4242, "duration": 4},
                "canvas": [1254, 1254], "crop": {"aspect": "1:1"}, "prompt": "p", "negative_prompt": "n"}
        good = project.add_take(shot.id, status=STATUS_CANCELLED, interrupted=True,
                                settings_snapshot=snap)
        old = project.add_take(shot.id, status=STATUS_CANCELLED, interrupted=True,  # pre-framing -> fail
                               settings_snapshot={"model_id": "seedance-2.0-std", "backend": "replicate"})
        # A take the USER cancelled (not interrupted) is restartable by snapshot but the bulk action
        # must SKIP it - it's only for crash/death-interrupted takes.
        user_cancelled = project.add_take(shot.id, status=STATUS_CANCELLED, interrupted=False,
                                          settings_snapshot=snap)
        # An interrupted FAILED take (in-flight render lost to the restart) IS picked up by the bulk
        # action alongside the cancelled ones.
        failed_orphan = project.add_take(shot.id, status=STATUS_FAILED, interrupted=True,
                                         settings_snapshot=snap)

        win = MainWindow(project)
        # Capture the runner build instead of firing a real backend; assert it gets the snapshot's
        # frozen shot + settings (seed 4242, the snapshot canvas/crop), not the live shot's.
        captured = {}

        def fake_make_runner(model, s, settings, tid):
            captured[tid] = {"model": model, "shot": s, "settings": settings}
            return lambda p: {}
        win._make_runner = fake_make_runner
        enqueued = []
        win.jobs.restart_take = lambda tid, backend, runner: enqueued.append(tid)
        win.save_project = lambda: True
        from ui import main_window as mw
        orig_confirm = mw.confirm_launch
        mw.confirm_launch = lambda parent, items: True
        try:
            win.restart_cancelled_takes()
        finally:
            mw.confirm_launch = orig_confirm

        assert enqueued == [good.id, failed_orphan.id], enqueued
        assert project.get_take(good.id).status == STATUS_PENDING
        assert project.get_take(failed_orphan.id).status == STATUS_PENDING, "failed orphan not restarted"
        cap = captured[good.id]
        assert cap["settings"]["seed"] == 4242
        assert cap["shot"].canvas_w == 1254 and cap["shot"].crop == {"aspect": "1:1"}
        assert cap["shot"].start_frame == str(start_png) and cap["shot"].prompt == "p"
        # The unrestartable old take is failed with a reason.
        failed = project.get_take(old.id)
        assert failed.status == STATUS_FAILED and "cannot restart" in (failed.error or "")
        # The user-cancelled take is left untouched - not enqueued, not failed.
        assert project.get_take(user_cancelled.id).status == STATUS_CANCELLED, "user cancel restarted!"
        assert user_cancelled.id not in enqueued

        # Context menu: a cancelled take gets a Restart entry, built without exec().
        project.update_take(good.id, status=STATUS_CANCELLED)   # the restart above flipped it to PENDING
        tv = TakesView(project, shot.id)
        restart_emits = []
        tv.restart_requested.connect(restart_emits.append)
        menu = tv._build_context_menu([good.id])
        labels = [a.text() for a in menu.actions()]
        assert any("Restart" in t for t in labels), labels
        next(a for a in menu.actions() if "Restart" in a.text()).trigger()
        assert restart_emits == [[good.id]], restart_emits
        # A non-cancelled selection has no Restart entry.
        project.update_take(good.id, status=STATUS_PENDING)
        assert not any("Restart" in a.text() for a in tv._build_context_menu([good.id]).actions())

        # A crash-interrupted FAILED take (in-flight render lost to an app/ComfyUI death) ALSO offers
        # a per-take Restart - mirroring the bulk action, which already picks it up (card #64).
        project.update_take(failed_orphan.id, status=STATUS_FAILED, interrupted=True)  # bulk flipped it
        restart_emits.clear()
        fo_menu = tv._build_context_menu([failed_orphan.id])
        assert any("Restart" in a.text() for a in fo_menu.actions()), [a.text() for a in fo_menu.actions()]
        next(a for a in fo_menu.actions() if "Restart" in a.text()).trigger()
        assert restart_emits == [[failed_orphan.id]], restart_emits
        # ...but a deliberately-FAILED (non-interrupted) take does NOT - a plain render failure is
        # not a restart candidate, only a crash-interrupted one is.
        plain_failed = project.add_take(shot.id, status=STATUS_FAILED, interrupted=False,
                                        settings_snapshot=snap)
        assert not any("Restart" in a.text()
                       for a in tv._build_context_menu([plain_failed.id]).actions())
        # The by-ids handler enforces the same gate: it forwards the FAILED+interrupted take and a
        # cancelled one to _restart_takes, but drops the deliberately-FAILED one.
        forwarded = {}
        win._restart_takes = lambda takes: forwarded.setdefault("ids", [t.id for t in takes])
        win._restart_takes_by_ids([failed_orphan.id, plain_failed.id, user_cancelled.id])
        assert forwarded["ids"] == [failed_orphan.id, user_cancelled.id], forwarded["ids"]
    finally:
        QMessageBox.information = orig_info
    print("restart from snapshot OK: in-place replay, unrestartable->failed, headless menu entry")


def test_interrupted_flag() -> None:
    """The `interrupted` flag separates crash/death-cancelled takes (the only ones the bulk Restart
    re-runs) from user-cancelled ones: it survives the takes.json round-trip, backfills on legacy
    load, and is set True by the auto paths (orphan recovery / abandon_local) but False by a manual
    cancel."""
    from PySide6.QtWidgets import QApplication
    from backends import recovery
    from backends.jobs import JobManager
    from store.models import STATUS_CANCELLED, STATUS_DONE, STATUS_FAILED, STATUS_PENDING, Take

    app = QApplication.instance() or QApplication([])  # noqa: F841
    project = Project.new()

    # Round-trip: interrupted survives _take_to_dict -> _take_from_dict.
    rt = Take(id="rt", shot_id="s", status=STATUS_CANCELLED, interrupted=True)
    assert project._take_from_dict(project._take_to_dict(rt)).interrupted is True

    # Migration backfill: a legacy cancelled/failed take (no `interrupted` key) infers it from the
    # orphan-recovery reason markers in `error`.
    def backfill(status, error):
        return project._take_from_dict(
            {"id": "m" + str(abs(hash(error)))[:6], "shot_id": "s", "status": status, "error": error}
        ).interrupted
    # Crash/death reasons -> True (each distinct recovery marker).
    assert backfill(STATUS_CANCELLED, "not submitted before restart; re-Generate to run it") is True
    assert backfill(STATUS_FAILED, "ComfyUI was unreachable at restart; render could not be recovered.") is True
    assert backfill(STATUS_FAILED, "no matching ComfyUI render found (lost to app restart)") is True
    assert backfill(STATUS_CANCELLED, "ComfyUI restart failed; pausing the local queue.") is True
    # User/genuine reasons -> False (crucially, a "cannot restart:" mark must NOT read as interrupted).
    assert backfill(STATUS_CANCELLED, "cancelled by user") is False
    assert backfill(STATUS_FAILED, "workflow error: bad node") is False
    assert backfill(STATUS_FAILED, "cannot restart: snapshot predates framing-in-snapshot") is False
    assert project._take_from_dict({"id": "c", "shot_id": "s", "status": STATUS_DONE}).interrupted is False

    # Manual cancel_take -> interrupted False.
    shot = project.add_shot("kick", model_id="seedance-2.0-std")
    manual = project.add_take(shot.id, status=STATUS_PENDING, settings_snapshot={"backend": "replicate"})
    jm = JobManager(project)
    jm.cancel_take(manual.id)
    assert project.get_take(manual.id).interrupted is False, "manual cancel must NOT be interrupted"

    # abandon_local (3-strike GPU crash) -> interrupted True.
    local = project.add_take(shot.id, status=STATUS_PENDING, settings_snapshot={"backend": "comfyui"})
    jm.abandon_local("ComfyUI crashed; pausing.")
    assert project.get_take(local.id).interrupted is True, "crash abandon must be interrupted"

    # Orphan recovery sets interrupted=True for BOTH its terminal actions, via _execute_plans:
    # CANCEL (a pending take never submitted) and FAIL (an in-flight render lost to the restart).
    from ui.main_window import MainWindow
    proj2 = Project.new()
    sh2 = proj2.add_shot("k", model_id="seedance-2.0-std")
    win = MainWindow(proj2)                 # built before any orphan exists -> no off-thread reconciler
    o_cancel = proj2.add_take(sh2.id, status=STATUS_PENDING, settings_snapshot={"backend": "comfyui"})
    o_fail = proj2.add_take(sh2.id, status=STATUS_GENERATING, settings_snapshot={"backend": "comfyui"})
    win._execute_plans([
        recovery.RecoveryPlan(o_cancel.id, sh2.id, recovery.CANCEL,
                              reason="queued but not submitted before restart"),
        recovery.RecoveryPlan(o_fail.id, sh2.id, recovery.FAIL,
                              reason="no matching ComfyUI render found (lost to app restart)"),
    ])
    gc, gf = proj2.get_take(o_cancel.id), proj2.get_take(o_fail.id)
    assert gc.interrupted is True and gc.status == STATUS_CANCELLED, (gc.interrupted, gc.status)
    assert gf.interrupted is True and gf.status == STATUS_FAILED, (gf.interrupted, gf.status)
    print("interrupted flag OK: round-trip, migration backfill, manual=False, recovery(cancel+fail)/abandon=True")


def test_ws_progress_diagnostics() -> None:
    """Crash-investigation diagnostics: ANIMGEN_NO_WS_PROGRESS disables the best-effort progress
    WS (bisection lever / escape hatch so it can't be implicated), and applog._max_stack_depth
    reports a positive depth + thread name (the watchdog's native-vs-Python overflow signal)."""
    import os as _os

    import applog
    from backends import comfy_client

    prev = _os.environ.get("ANIMGEN_NO_WS_PROGRESS")
    _os.environ["ANIMGEN_NO_WS_PROGRESS"] = "1"
    try:
        t, stop = comfy_client._start_progress_ws("cid", "pid", lambda **k: None)
        assert t is None and stop is None, (t, stop)
    finally:
        if prev is None:
            _os.environ.pop("ANIMGEN_NO_WS_PROGRESS", None)
        else:
            _os.environ["ANIMGEN_NO_WS_PROGRESS"] = prev

    depth, who = applog._max_stack_depth()
    assert isinstance(depth, int) and depth > 0 and isinstance(who, str), (depth, who)
    print("ws diagnostics OK: NO_WS_PROGRESS disables listener; stack-depth probe reports")


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


def test_select_rows() -> None:
    from ui.queue_view import select_rows
    from store.models import STATUS_CANCELLED

    gen = Take(id="g", shot_id="s", status=STATUS_GENERATING)
    pend = Take(id="p", shot_id="s", status=STATUS_PENDING)
    # Finished takes with OUT-OF-ORDER completion stamps: list order (d, f, c) deliberately
    # differs from completion order (f newest, then c, then d) so the ordering assertion
    # tests "by completion time", not "by list position" (the bug card #74 fixed).
    done = Take(id="d", shot_id="s", status=STATUS_DONE, completed="2026-06-18T10:00:00")
    fail = Take(id="f", shot_id="s", status=STATUS_FAILED, completed="2026-06-18T12:00:00")
    canc = Take(id="c", shot_id="s", status=STATUS_CANCELLED, completed="2026-06-18T11:00:00")
    takes = [done, gen, fail, pend, canc]

    # Active first (generating, then pending), then finished by completion time, newest first.
    ids = [t.id for t in select_rows(takes)]
    assert ids == ["g", "p", "f", "c", "d"], ids

    # Clearing dismisses every finished take but leaves active ones in.
    dismissed = {t.id for t in takes if t.status in (STATUS_DONE, STATUS_FAILED, STATUS_CANCELLED)}
    ids = [t.id for t in select_rows(takes, dismissed)]
    assert ids == ["g", "p"], ids

    # A finished take that appears *after* a clear (not in dismissed) still shows.
    newdone = Take(id="d2", shot_id="s", status=STATUS_DONE, completed="2026-06-18T13:00:00")
    ids = [t.id for t in select_rows(takes + [newdone], dismissed)]
    assert ids == ["g", "p", "d2"], ids

    # An active take is never hidden even if its id is (wrongly) in dismissed; finished
    # stay ordered newest-first by completion.
    ids = [t.id for t in select_rows(takes, {"g", "p"})]
    assert ids == ["g", "p", "f", "c", "d"], ids

    # The core fix: a RESTARTED take finishes at its original (earlier) list position but
    # with the newest `completed`, so it must surface on TOP of the finished section -
    # under the old slice-by-list-position it stayed buried and the section showed stale rows.
    restarted = Take(id="r", shot_id="s", status=STATUS_DONE, completed="2026-06-18T14:00:00")
    ids = [t.id for t in select_rows([restarted, done, fail, canc])]
    assert ids == ["r", "f", "c", "d"], ids

    # Missing `completed` (e.g. some cancelled takes) falls back to `created` for ordering.
    c_old = Take(id="co", shot_id="s", status=STATUS_CANCELLED, created="2026-06-18T08:00:00")
    c_new = Take(id="cn", shot_id="s", status=STATUS_CANCELLED, created="2026-06-18T09:00:00")
    ids = [t.id for t in select_rows([c_old, c_new])]
    assert ids == ["cn", "co"], ids

    # Same-second completion (timestamps are second-precision): the tie must break
    # deterministically by `created` desc (newest-created on top), NOT silently fall back to
    # input/list order - that fallback is the very bug this fix removes.
    t_a = Take(id="a", shot_id="s", status=STATUS_DONE,
               completed="2026-06-18T15:00:00", created="2026-06-18T14:00:00")
    t_b = Take(id="b", shot_id="s", status=STATUS_DONE,
               completed="2026-06-18T15:00:00", created="2026-06-18T14:30:00")
    assert [t.id for t in select_rows([t_a, t_b])] == ["b", "a"]   # newer created on top
    assert [t.id for t in select_rows([t_b, t_a])] == ["b", "a"]   # independent of input order

    # Fully tied (same completed AND created): the `id` tiebreak still makes the order
    # independent of input order, so a same-second cluster never flickers between rebuilds.
    t_c = Take(id="c1", shot_id="s", status=STATUS_DONE,
               completed="2026-06-18T15:00:00", created="2026-06-18T14:00:00")
    t_d = Take(id="c2", shot_id="s", status=STATUS_DONE,
               completed="2026-06-18T15:00:00", created="2026-06-18T14:00:00")
    assert [t.id for t in select_rows([t_c, t_d])] == [t.id for t in select_rows([t_d, t_c])]

    # No cap: every finished take shows (the old 15-row limit is gone), newest-first.
    many = [Take(id=f"x{i}", shot_id="s", status=STATUS_DONE,
                 completed=f"2026-06-18T{i:02d}:00:00") for i in range(20)]
    ids = [t.id for t in select_rows(many)]
    assert len(ids) == 20, len(ids)
    assert ids == [f"x{i}" for i in range(19, -1, -1)], ids
    print("select_rows OK: active-first, finished newest-by-completion, created fallback, "
          "clear hides finished, active never hidden, no cap")


def test_queue_summary() -> None:
    """The Queue header: a cumulative 'N done' counter alongside running/queued/failed, plus
    the 'Last finished' strip text for the newest finished take (card #77)."""
    from store.models import (STATUS_CANCELLED, STATUS_DONE, STATUS_FAILED,
                              STATUS_GENERATING, STATUS_PENDING)
    from ui.queue_view import last_finished, last_finished_text, select_rows, summary_line

    gen = Take(id="g", shot_id="s", status=STATUS_GENERATING)
    pend = Take(id="p", shot_id="s", status=STATUS_PENDING)
    done = Take(id="d", shot_id="s", status=STATUS_DONE,
                started="2026-06-18T10:00:00", completed="2026-06-18T10:00:12")
    done2 = Take(id="d2", shot_id="s", status=STATUS_DONE, completed="2026-06-18T11:00:00")
    fail = Take(id="f", shot_id="s", status=STATUS_FAILED, completed="2026-06-18T09:00:00")
    takes = [gen, pend, done, done2, fail]

    # Counter: running/queued always; done + failed appended only when nonzero, in that order.
    assert summary_line(takes, True) == "1 running · 1 queued · 2 done · 1 failed", summary_line(takes, True)
    assert summary_line([gen, pend], True) == "1 running · 1 queued"            # done/failed omitted at 0
    assert summary_line([done], True) == "0 running · 0 queued · 1 done"        # done shown, failed omitted
    assert summary_line(takes, False) == "Queue empty - nothing generating or queued."  # empty wins

    # last_finished: newest finished among the (already-ordered) rows = the first finished row.
    rows = select_rows(takes)
    assert last_finished(rows).id == "d2", last_finished(rows).id               # newest completion
    assert last_finished([gen, pend]) is None                                   # none finished

    # last_finished_text: a done take shows render duration; a failed/cancelled shows its word.
    assert last_finished_text(done, "Kick") == "Last finished: Kick - done in 12s", last_finished_text(done, "Kick")
    assert last_finished_text(done2, "Kick") == "Last finished: Kick - done"    # no started -> no elapsed
    assert last_finished_text(fail, "Punch") == "Last finished: Punch - failed"
    canc = Take(id="c", shot_id="s", status=STATUS_CANCELLED)
    assert last_finished_text(canc, "Block") == "Last finished: Block - cancelled"

    # Deliberate divergence after Clear: the done/failed counter spans ALL project takes (a
    # cumulative tally, the same all-takes basis 'N failed' always used), while the strip reads
    # the dismissed-filtered rows. So a clear (all finished dismissed) empties the strip yet the
    # counter still reports the project totals - pin it so a future change doesn't silently flip
    # the counter to rows-based.
    cleared_rows = select_rows(takes, dismissed={done.id, done2.id, fail.id})
    assert [t.id for t in cleared_rows] == ["g", "p"]                          # only active survive
    assert last_finished(cleared_rows) is None                                 # strip hidden
    assert summary_line(takes, bool(cleared_rows)) == "1 running · 1 queued · 2 done · 1 failed"
    print("queue_summary OK: done counter, last-finished strip text, clear divergence")


def test_queue_actions_in_queue_tab() -> None:
    """The three generation-queue actions (Pause batch / Cancel pending / Restart interrupted
    takes) render in the Queue tab header, not the Shots-tab control strip. The QActions are
    still owned by MainWindow (so every _refresh_*_action site keeps driving them); QueueView
    just shows them as QToolButtons whose defaultAction is the MainWindow-owned action."""
    from PySide6.QtWidgets import QApplication, QToolBar, QToolButton

    from ui.main_window import MainWindow

    QApplication.instance() or QApplication([])
    win = MainWindow(Project.new("Untitled"))
    acts = [win.pause_act, win.cancel_act, win.restart_act]

    # Each action is rendered as a QToolButton inside the Queue tab.
    queue_btn_actions = {b.defaultAction() for b in win.queue_tab.findChildren(QToolButton)}
    for act in acts:
        assert act in queue_btn_actions, f"{act.text()!r} missing from the Queue tab header"

    # ...and NOT present in any Shots-tab toolbar anymore.
    shots_toolbar_actions = set()
    for tb in win.shots_tab.findChildren(QToolBar):
        shots_toolbar_actions.update(tb.actions())
    for act in acts:
        assert act not in shots_toolbar_actions, f"{act.text()!r} still in the Shots toolbar"

    win.close()
    print("queue_actions_in_queue_tab OK: pause/cancel/restart moved to Queue header")


def test_quiesce_on_switch() -> None:
    """JobManager.quiesce (project switch): cancels the outgoing project's PENDING takes
    (interrupted=True -> restartable), clears the stale _cancelled/_stopping/_requeue id sets,
    but LEAVES the currently-GENERATING take running and keeps its _runners entry so
    live_take_ids still names it after the switch."""
    import threading
    import time

    from PySide6.QtWidgets import QApplication

    from backends.jobs import JobManager
    from store.models import STATUS_CANCELLED, STATUS_DONE, STATUS_GENERATING, STATUS_PENDING

    app = QApplication.instance() or QApplication([])
    project = Project.new()
    shot = project.add_shot("kick", model_id="local-flf-wan14b")
    jm = JobManager(project)

    local_snap, hosted_snap = {"backend": "comfyui"}, {"backend": "replicate"}
    release = threading.Event()
    active = project.add_take(shot.id, status=STATUS_PENDING, settings_snapshot=local_snap)
    lq = project.add_take(shot.id, status=STATUS_PENDING, settings_snapshot=local_snap)
    hq = project.add_take(shot.id, status=STATUS_PENDING, settings_snapshot=hosted_snap)

    def blocker(progress):
        release.wait(timeout=10)
        return {"video_path": "x.mp4"}

    jm.enqueue(active.id, "comfyui", blocker)
    jm.enqueue(lq.id, "comfyui", blocker)   # queues behind `active` (serialized local pool)
    # hq is left PENDING but NOT dispatched (the hosted pool would run it immediately) - it
    # stands for a queued hosted take that quiesce should also cancel.

    for _ in range(200):
        if project.get_take(active.id).status == STATUS_GENERATING:
            break
        time.sleep(0.02)
    assert project.get_take(active.id).status == STATUS_GENERATING, "blocker never started"

    # Seed stale id-set membership that quiesce must prune, plus a live-worker stop flag it
    # must PRESERVE (a request_stop just before the switch still records CANCELLED).
    jm._cancelled.add("stale-c")
    jm._stopping.add("stale-s")
    jm._stopping.add(active.id)
    jm._requeue.add("stale-r")
    assert jm.has_in_flight_work() is True

    n = jm.quiesce()
    assert n == 2, n                                     # both PENDING takes (local + hosted)
    assert project.get_take(lq.id).status == STATUS_CANCELLED
    assert project.get_take(hq.id).status == STATUS_CANCELLED
    assert project.get_take(lq.id).interrupted is True, "switch-cancel must be restartable"
    assert project.get_take(active.id).status == STATUS_GENERATING, "running take must survive"
    # Stale ids are pruned; the just-cancelled ids stay in _cancelled (the same dequeue-race
    # safety net as cancel_pending), the live worker keeps its _stopping flag, and the
    # GENERATING take's runner entry is KEPT for live_take_ids.
    assert jm._cancelled == {lq.id, hq.id}, jm._cancelled
    assert jm._stopping == {active.id}, jm._stopping
    assert jm._requeue == set(), jm._requeue
    assert active.id in jm.live_take_ids(), "generating take dropped from live set"
    assert lq.id not in jm.live_take_ids() and hq.id not in jm.live_take_ids()
    jm._stopping.discard(active.id)   # let the blocker finish DONE below, not CANCELLED

    # The still-running worker finishes and records DONE via its own project reference.
    release.set()
    assert jm.wait_for_done(10000), "jobs did not finish"
    app.processEvents()
    assert project.get_take(active.id).status == STATUS_DONE, "left-running take must complete"
    assert active.id not in jm.live_take_ids(), "_on_job_done should drop the finished take"
    print("quiesce OK: pending cancelled (interrupted), stale ids pruned, running take survives")


def test_switch_skips_live_orphans() -> None:
    """recovery.comfy_orphans excludes ids a live worker is still rendering (A->B->A): the take
    the original worker owns is NOT reconciled as an orphan, so no second monitor races it."""
    from backends import recovery
    from store.models import STATUS_GENERATING, STATUS_PENDING

    project = Project.new()
    shot = project.add_shot("kick", model_id="local-flf-wan14b")
    snap = {"backend": "comfyui"}
    live = project.add_take(shot.id, status=STATUS_GENERATING, seed=1, settings_snapshot=snap)
    dead = project.add_take(shot.id, status=STATUS_GENERATING, seed=2, settings_snapshot=snap)
    pend = project.add_take(shot.id, status=STATUS_PENDING, seed=3, settings_snapshot=snap)

    # No skip set: all three mid-flight takes are orphan candidates.
    assert {o.id for o in recovery.comfy_orphans(project)} == {live.id, dead.id, pend.id}
    # With the live worker's take in the skip set, it is excluded; the rest still reconcile.
    got = {o.id for o in recovery.comfy_orphans(project, {live.id})}
    assert got == {dead.id, pend.id}, got
    print("comfy_orphans skip OK: live-worker take excluded, real orphans still reconciled")


def test_switch_resets_batch_and_pause() -> None:
    """MainWindow._switch_project drops a batch tracked against the OLD project and lifts a
    transient local pause, so a stale batch can't finalize (empty report + power action) after
    the switch. The drain prompt is driven headlessly (no exec()); an abort returns False and
    leaves the current project untouched."""
    from PySide6.QtWidgets import QApplication

    from backends import batch
    from ui.main_window import MainWindow
    from store.models import STATUS_PENDING

    app = QApplication.instance() or QApplication([])  # noqa: F841
    project = Project.new("A")
    shot = project.add_shot("kick", model_id="seedance-2.0-std")
    win = MainWindow(project)
    # The batch below uses POWER_SLEEP so a regression is observable - but the power action
    # must NEVER actually run in a test (it stops ComfyUI and puts the PC to sleep). Capture
    # instead of executing; the assertion below proves the switch never reaches it.
    power_calls = []
    win._perform_power_action = power_calls.append

    # --- abort path: in-flight work + user says No -> switch is refused, project unchanged.
    take = project.add_take(shot.id, status=STATUS_PENDING,
                            settings_snapshot={"backend": "replicate"})
    win._batch = batch.BatchRun(take_ids={take.id}, power_action=batch.POWER_SLEEP, started="t")
    win._confirm_switch_with_queued_work = lambda: False
    other = Project.new("B")
    assert win._switch_project(other) is False
    assert win.project is project, "aborted switch must not swap the project"
    assert win._batch is not None, "aborted switch must not drop the batch"

    # --- proceed path: user says Yes -> queued take cancelled, batch + pause reset.
    win._confirm_switch_with_queued_work = lambda: True
    win._stop_paused_local = True
    assert win._switch_project(other) is True
    assert win.project is other, "confirmed switch should swap the project"
    assert win._batch is None, "batch must be dropped on switch (no stale power action)"
    # The batch must be dropped BEFORE quiesce's CANCELLED emits: otherwise cancelling the
    # last pending take drains the batch to complete mid-switch and _finalize_batch fires
    # the sleep/stop-ComfyUI power action while the user is switching projects.
    assert power_calls == [], f"power action fired during switch: {power_calls}"
    assert win._stop_paused_local is False, "transient local pause must be lifted on switch"
    assert win.jobs.is_local_paused() is False
    # The old project's queued take was cancelled by quiesce and flagged restartable.
    from store.models import STATUS_CANCELLED
    assert project.get_take(take.id).status == STATUS_CANCELLED
    assert project.get_take(take.id).interrupted is True

    win.close()
    print("switch reset OK: abort intact; proceed drops batch first (no power action) + lifts pause")


if __name__ == "__main__":
    test_build_input()
    test_capability_sync()
    test_resolve_enums()
    test_app_settings()
    test_store_absent_vs_unreadable()
    test_roster_integrity()
    test_comfy_prepare()
    test_dynamic_vram_gate()
    test_preflight_gate()
    test_comfy_launch_helpers()
    test_comfy_gpu_cache()
    test_comfy_stop_helpers()
    test_comfy_api_errors()
    test_poll_retry()
    test_claim_output_empty()
    test_stop_work_targets_prompt()
    test_comfy_views()
    test_orphan_recovery()
    test_crash_recovery()
    test_wait_until_responsive()
    test_restart_server()
    test_ensure_server()
    test_abandon_local()
    test_pause_resume_local()
    test_pause_requeue_current()
    test_clear_local_pause()
    test_stop_pauses_nonbatch_local()
    test_stop_handler_nonbatch()
    test_total_price()
    test_cost_summary()
    test_launch_label()
    test_subcent_cost_display()
    test_cancel_pending()
    test_cancel_shot_takes()
    test_inflight_stop_maps_to_cancelled()
    test_recovered_crash_maps_to_interrupted()
    test_request_stop_calls_backend()
    test_is_stop_requested()
    test_job_manager()
    test_progress_fraction()
    test_sampler_step_plan()
    test_client_id_in_queue()
    test_progress_pct()
    test_done_elapsed()
    test_select_rows()
    test_queue_summary()
    test_queue_actions_in_queue_tab()
    test_batch()
    test_batch_finalize()
    test_restart_plan()
    test_restart_take()
    test_restart_from_snapshot()
    test_interrupted_flag()
    test_ws_progress_diagnostics()
    test_quiesce_on_switch()
    test_switch_skips_live_orphans()
    test_switch_resets_batch_and_pause()
    print("PHASE 2 SMOKE: PASS")
