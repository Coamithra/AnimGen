"""Local (ComfyUI) generation backend.

Wraps scripts/run_workflow.py's submit/poll loop as importable functions that RAISE
(ComfyError) and report progress. prepare_workflow() clones a template and sets the
start/end keypose (LoadImage), prompt/negative (CLIPTextEncode) and seed nodes -
using an explicit node-role map from the model library when available, else a
heuristic (ascending node-id ordering). Keyposes are copied into ComfyUI's input/
dir (LoadImage reads from there). dry_run prepares the workflow WITHOUT submitting.
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Callable, Optional

from paths import COMFY_DIR, COMFY_INPUT_DIR, DATA_DIR

COMFY_PORT = 8188
COMFY_URL = f"http://127.0.0.1:{COMFY_PORT}"
COMFY_OUTPUT_DIR = COMFY_DIR / "output"

# A progress callback takes either progress(line) for a milestone log line, or
# progress(frac=.., label=..) for a 0..1 completion fraction (the WS step progress below).
ProgressCb = Optional[Callable[..., None]]

# Diagnostics for the best-effort progress WebSocket (suspect in the 2026-06-18 native
# stack-overflow crash). The listener logs frame telemetry to animgen.log so the tail
# right before a crash shows what it was doing; ANIMGEN_NO_WS_PROGRESS=1 disables the
# listener entirely (renders still complete via the /history poll - rule #11) as both a
# safe escape hatch and a bisection lever (run a batch with it off; if the crash stops,
# the WS is the culprit).
_ws_logger = logging.getLogger("animgen.comfy.ws")
_WS_TELEMETRY_S = 30.0          # how often the listener logs a frame-count summary


class ComfyError(RuntimeError):
    pass


def _log(cb: ProgressCb, msg: str) -> None:
    if cb:
        cb(msg)


def _http_error_detail(e: "urllib.error.HTTPError") -> str:
    """Best-effort node-level error text pulled from a ComfyUI HTTP error body.

    A /prompt validation failure (invalid workflow / missing model file) is a 400 whose
    body carries {error, node_errors}; surfacing that beats the generic 'unreachable'
    message, which used to swallow it (HTTPError is a URLError subclass). Falls back to a
    truncated raw body, then to just the status line, if the body isn't the expected JSON.
    """
    try:
        raw = e.read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 - the body may already be consumed / unreadable
        raw = ""
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return raw.strip()[:1500]
    if isinstance(payload, dict):
        parts = []
        err = payload.get("error")
        if isinstance(err, dict):
            parts.append(str(err.get("message") or err.get("type") or err))
        elif err:
            parts.append(str(err))
        node_errors = payload.get("node_errors")
        if node_errors:
            parts.append(f"node_errors: {json.dumps(node_errors)[:1000]}")
        if parts:
            return " | ".join(parts)
    return json.dumps(payload)[:1500]


def _api(path: str, data=None, timeout: int = 30) -> dict:
    req = urllib.request.Request(COMFY_URL + path)
    if data is not None:
        req.add_header("Content-Type", "application/json")
        req.data = json.dumps(data).encode()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:  # a real HTTP response (4xx/5xx), NOT unreachable
        raise ComfyError(f"ComfyUI HTTP {e.code} on {path}: {_http_error_detail(e)}") from e
    except urllib.error.URLError as e:  # type: ignore[name-defined]
        raise ComfyError(f"ComfyUI unreachable at {COMFY_URL} ({e}). Is it running?") from e
    except socket.timeout as e:  # read timed out mid-body (server alive but stalled)
        raise ComfyError(f"ComfyUI timed out on {path} after {timeout}s. "
                         "It may be busy loading weights.") from e
    except (json.JSONDecodeError, ValueError) as e:  # truncated / non-JSON body
        raise ComfyError(f"ComfyUI returned a malformed response on {path} ({e}).") from e


# Dynamic VRAM (ComfyUI's comfy-aimdo engine, default-ON) streams model weights
# RAM<->VRAM mid-kernel over PCIe. On a 12GB card under heavy offload a 14B render can
# stall one GPU op past Windows' 2s TDR watchdog -> driver reset -> ComfyUI dies
# mid-render with no traceback (cost the Fighter project a night of crashes, 2026-06-14;
# see ../Fighter/research/comfyui-gpu-watchdog-crash-and-aimdo.md). The fix is a server
# LAUNCH flag (--disable-dynamic-vram). AnimGen doesn't start ComfyUI, so we can't pass
# it - but /system_stats echoes the server's argv, so we can REFUSE to run a local job
# against a server that has dynamic VRAM enabled. Bypass with ANIMGEN_ALLOW_DYNAMIC_VRAM=1.

# Flags that switch dynamic VRAM off, mirroring ComfyUI's enables_dynamic_vram() gate
# (comfy/cli_args.py): dynamic VRAM is ON unless one of these is on the command line.
_DYNAMIC_VRAM_DISABLERS = frozenset({
    "--disable-dynamic-vram", "--highvram", "--gpu-only", "--novram", "--cpu",
})


def dynamic_vram_enabled(argv: list[str]) -> bool:
    """True if a ComfyUI launched with `argv` has dynamic VRAM (aimdo) enabled.

    Pure mirror of ComfyUI's `enables_dynamic_vram()`: ON by default, off only when a
    disabling flag is present. Split out from preflight() so it's unit-testable offline.
    """
    return not (_DYNAMIC_VRAM_DISABLERS & set(argv))


# Async weight offloading is a SECOND, independent mid-kernel PCIe weight-streaming path.
# It is NOT the aimdo dynamic-VRAM engine and is NOT gated by enables_dynamic_vram(), so
# --disable-dynamic-vram does NOT turn it off. ComfyUI enables it by default on Nvidia/AMD
# (comfy/model_management.py: NUM_STREAMS=2) unless --disable-async-offload (or --cpu, or an
# explicit --async-offload 0) is given. It streams weights RAM<->VRAM the same way aimdo
# does, so it can stall a 14B op past Windows' 2s TDR watchdog just as well - it was the
# actual trigger of the 2026-06-17 overnight-batch kill (banner: "Using async weight
# offloading with 2 streams"). The guard must refuse it too, not just dynamic VRAM.


def _async_offload_value(argv: list[str]) -> Optional[int]:
    """The explicit --async-offload stream count in `argv`, or None if the flag is absent.

    Mirrors ComfyUI's argparse (nargs='?', const=2): bare `--async-offload` -> 2,
    `--async-offload N` / `--async-offload=N` -> N, a non-int/missing value -> 2.
    """
    for i, tok in enumerate(argv):
        if tok == "--async-offload":
            nxt = argv[i + 1] if i + 1 < len(argv) else None
            if nxt is None:
                return 2          # bare trailing flag -> const 2
            try:
                return int(nxt)   # explicit count (incl. 0 / negative, like argparse)
            except ValueError:
                return 2          # next token isn't a count (e.g. another flag) -> const 2
        if tok.startswith("--async-offload="):
            try:
                return int(tok.split("=", 1)[1])
            except ValueError:
                return 2
    return None


def async_offload_enabled(argv: list[str], device_type: Optional[str] = None) -> bool:
    """True if a ComfyUI launched with `argv` streams weights via async offload.

    Mirror of ComfyUI's NUM_STREAMS computation: an explicit --async-offload sets the count;
    --disable-async-offload (or --cpu) forces it off; otherwise it defaults ON (2 streams) on
    a GPU device, which Nvidia *and* AMD report to torch as device.type 'cuda'. `device_type`
    is /system_stats devices[0].type. When it's unknown (None) we assume a GPU - the
    conservative, refuse-by-default reading, since the silent default-on case is what bit us.
    """
    if "--cpu" in argv:
        return False
    if "--disable-async-offload" in argv:
        return False
    val = _async_offload_value(argv)
    if val is not None:
        return val > 0
    return device_type in (None, "cuda")


def preflight(progress_cb: ProgressCb = None) -> None:
    """Abort before launching a local job if the running ComfyUI streams weights over PCIe.

    Queries /system_stats (which echoes the server's launch argv + device type) and raises
    ComfyError if EITHER mid-kernel PCIe weight-streaming path is active - dynamic VRAM
    (aimdo) or async weight offloading - since both can stall a 14B op past the 2s TDR
    watchdog. No-op when ANIMGEN_ALLOW_DYNAMIC_VRAM is set. Doubles as a reachability check
    (clear error if the server is down).
    """
    if os.environ.get("ANIMGEN_ALLOW_DYNAMIC_VRAM"):
        _log(progress_cb, "preflight: TDR weight-streaming guard bypassed (ANIMGEN_ALLOW_DYNAMIC_VRAM)")
        return
    stats = _api("/system_stats")
    argv = stats.get("system", {}).get("argv", [])
    device_type = (stats.get("devices") or [{}])[0].get("type")
    active = []
    if dynamic_vram_enabled(argv):
        active.append("dynamic VRAM (aimdo)")
    if async_offload_enabled(argv, device_type):
        active.append("async weight offloading")
    if active:
        raise ComfyError(
            f"ComfyUI is streaming model weights over PCIe ({' + '.join(active)}). On the "
            "12GB card this trips Windows' 2s GPU watchdog (TDR) on 14B renders and kills "
            "the server mid-job - and a driver reset can take AnimGen down with it. Restart "
            "ComfyUI with --disable-dynamic-vram --disable-async-offload - e.g. use the "
            "Launch ComfyUI button or run scripts/launch_comfyui.py (or .bat), which apply "
            "both. Set ANIMGEN_ALLOW_DYNAMIC_VRAM=1 to bypass this guard. Details: "
            "../Fighter/research/comfyui-gpu-watchdog-crash-and-aimdo.md."
        )
    _log(progress_cb, "preflight: weight streaming disabled (dynamic VRAM + async offload) - OK")


# --- Server process management ----------------------------------------------
# AnimGen can start the local ComfyUI itself (the "Launch ComfyUI" button / the
# scripts/launch_comfyui.py CLI) so --disable-dynamic-vram is always applied.
# build_launch_command() is the single source of truth for HOW to start it.

# --disable-dynamic-vram + --disable-async-offload: turn OFF both mid-kernel PCIe
#   weight-streaming paths (aimdo dynamic VRAM, and async weight offloading - the second,
#   default-on-Nvidia/AMD path that --disable-dynamic-vram does NOT cover). Either can stall
#   a 14B op past the 2s GPU watchdog -> TDR -> server (and possibly AnimGen) dies (see above).
# --cache-none: re-execute every node each run instead of caching model results in VRAM.
#   On the 12GB card the dual-14B Wan workflow (~24GB of weights through 12GB) can't keep
#   the prior run's models resident, so caching just leaves ~4-5GB pinned that the next
#   render then spills to system RAM over PCIe (the 8 -> 36 s/it slowdown). Dropping the
#   cache fully unloads each model before the next loads, so each 9GB expert gets the whole
#   card. Costs a few seconds of reload per run; saves the per-step PCIe streaming.
REQUIRED_FLAGS = ["--disable-dynamic-vram", "--disable-async-offload", "--cache-none"]
_DEFAULT_FLAGS = [("--listen", "127.0.0.1"), ("--port", str(COMFY_PORT))]  # (flag, value)
_server_proc: "Optional[subprocess.Popen]" = None  # the ComfyUI we launched, if any

# --- Isolated GPU (CUDA JIT kernel) cache -------------------------------------------------
# ComfyUI/PyTorch JIT-compile CUDA kernels (PTX -> SASS) and cache them on disk. By default
# they land in the GLOBAL, shared %APPDATA%/NVIDIA/ComputeCache (or ~/.nv/ComputeCache),
# mixed in with every other CUDA app and not cleanly identifiable as "videogen's". We point a
# launched ComfyUI's CUDA_CACHE_PATH at a project-local, gitignored folder (data/gpu_cache)
# and cap it (CUDA_CACHE_MAXSIZE), so videogen owns a single, identifiable, bounded,
# one-command-wipeable shader cache (clear_gpu_cache() / scripts/clear_gpu_cache.py). This is
# the CUDA *compute* cache ONLY - it is unrelated to the DirectX DXCache (a desktop/browser/
# Electron cache; AnimGen forces software GL, rule #15, so its own UI writes neither).
GPU_CACHE_DIR = DATA_DIR / "gpu_cache"
GPU_CACHE_MAXSIZE_BYTES = 2 * 1024 ** 3   # 2 GiB ceiling; CUDA evicts oldest past this (max 4 GiB)


def _legacy_compute_caches() -> "list[Path]":
    """The default (pre-isolation) global CUDA compute-cache locations, if derivable."""
    out: "list[Path]" = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        out.append(Path(appdata) / "NVIDIA" / "ComputeCache")
    out.append(Path.home() / ".nv" / "ComputeCache")
    return out


def launch_env() -> "dict[str, str]":
    """Environment for a launched ComfyUI: the inherited env plus an isolated, capped CUDA
    kernel-cache location (GPU_CACHE_DIR) so videogen's GPU shader cache is identifiable and
    wipeable instead of accumulating in the global ComputeCache. A CUDA_CACHE_PATH /
    CUDA_CACHE_MAXSIZE already set in the environment is respected (setdefault)."""
    env = dict(os.environ)
    try:
        GPU_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass  # if we can't create it, fall through - CUDA uses its default location
    env.setdefault("CUDA_CACHE_PATH", str(GPU_CACHE_DIR))
    env.setdefault("CUDA_CACHE_MAXSIZE", str(GPU_CACHE_MAXSIZE_BYTES))
    return env


def gpu_cache_size_mb(include_legacy: bool = False) -> float:
    """Total size (MB) of videogen's isolated GPU cache; with include_legacy, also the global
    pre-isolation ComputeCache(s). Cheap stat walk for the confirm dialog / reporting."""
    dirs = [GPU_CACHE_DIR] + (_legacy_compute_caches() if include_legacy else [])
    total = 0
    for d in dirs:
        if not d.exists():
            continue
        for p in d.rglob("*"):
            if p.is_file():
                try:
                    total += p.stat().st_size
                except OSError:
                    pass
    return total / (1024 * 1024)


def clear_gpu_cache(include_legacy: bool = False) -> dict:
    """Delete videogen's isolated CUDA kernel cache (GPU_CACHE_DIR). Best-effort: a file the
    running server holds open is skipped, not fatal (CUDA recompiles it on demand). With
    include_legacy, also clears the global pre-isolation ComputeCache(s). Returns the
    {files, bytes} actually removed."""
    dirs = [GPU_CACHE_DIR] + (_legacy_compute_caches() if include_legacy else [])
    files = 0
    freed = 0
    for d in dirs:
        if not d.exists():
            continue
        for p in d.rglob("*"):
            if p.is_file():
                try:
                    sz = p.stat().st_size
                    p.unlink()
                    files += 1
                    freed += sz
                except OSError:
                    pass  # locked / in use - skip (best-effort)
    return {"files": files, "bytes": freed}


def comfy_python() -> Path:
    """The ComfyUI venv interpreter, falling back to the current interpreter."""
    venv_py = COMFY_DIR / "venv" / "Scripts" / "python.exe"
    return venv_py if venv_py.exists() else Path(sys.executable)


def build_launch_command(extra: Optional[list[str]] = None) -> list[str]:
    """ComfyUI launch argv with our required flags. A default flag/value pair is dropped
    whole when `extra` overrides that flag (so e.g. --port won't orphan its default 8188)."""
    extra = list(extra or [])
    cmd = [str(comfy_python()), str(COMFY_DIR / "main.py")]
    for flag, value in _DEFAULT_FLAGS:
        if flag not in extra:
            cmd += [flag, value]
    cmd += [f for f in REQUIRED_FLAGS if f not in extra]
    cmd += extra
    return cmd


def server_status(timeout: int = 2) -> dict:
    """Non-raising probe of the local ComfyUI server.

    Returns {running, version, dynamic_vram, async_offload, argv}. dynamic_vram and
    async_offload are None when the server is down, else True/False derived from its launch
    argv (+ device type); the two together are the PCIe-weight-streaming gate preflight enforces.
    """
    try:
        stats = _api("/system_stats", timeout=timeout)
    except Exception:  # noqa: BLE001 - a status probe must never raise
        return {"running": False, "version": None, "dynamic_vram": None,
                "async_offload": None, "argv": []}
    system = stats.get("system", {})
    argv = system.get("argv", [])
    device_type = (stats.get("devices") or [{}])[0].get("type")
    return {"running": True, "version": system.get("comfyui_version"),
            "dynamic_vram": dynamic_vram_enabled(argv),
            "async_offload": async_offload_enabled(argv, device_type), "argv": argv}


def launch_server(extra: Optional[list[str]] = None) -> "subprocess.Popen":
    """Start a detached local ComfyUI with --disable-dynamic-vram, logging to
    data/comfyui_server.log. Returns the Popen handle without waiting for readiness.
    Raises ComfyError if the ComfyUI install isn't found."""
    global _server_proc
    if not (COMFY_DIR / "main.py").exists():
        raise ComfyError(f"ComfyUI not found at {COMFY_DIR} (set ANIMGEN_COMFY_DIR).")
    cmd = build_launch_command(extra)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    logfile = open(DATA_DIR / "comfyui_server.log", "ab")  # inherited by the child
    flags = 0
    if sys.platform == "win32":  # detach: no console window, outlives AnimGen
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, "DETACHED_PROCESS", 0x8)
    try:
        proc = subprocess.Popen(cmd, cwd=str(COMFY_DIR), stdout=logfile,
                                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                env=launch_env(),  # isolated, capped CUDA kernel cache
                                creationflags=flags, close_fds=True)
    finally:
        logfile.close()  # the child keeps its own inherited handle
    _server_proc = proc  # remembered so stop_server() can terminate exactly this one
    return proc


def wait_until_responsive(timeout_s: int = 120, poll_s: float = 2.0,
                          is_alive: Optional[Callable[[], bool]] = None) -> bool:
    """Block until the local ComfyUI answers /system_stats (running) or `timeout_s` elapses.

    Returns whether it came up. Used by restart_server after a crash relaunch. Each probe
    eats a full socket timeout while the port is still closed (SYNs to a closed port are
    dropped, not refused, on this machine - see CLAUDE.md), so server_status's own short
    timeout doubles as the inter-poll wait; poll_s is a small extra breather between probes.

    is_alive (optional) is the liveness of the process we're waiting on: when it returns
    False we bail out immediately rather than polling a dead server for the full timeout
    (e.g. a relaunch that couldn't bind the port and exited at once).
    """
    deadline = time.time() + timeout_s
    while True:
        if server_status(timeout=2)["running"]:
            return True
        if is_alive is not None and not is_alive():
            return False              # the process we were waiting on died; stop polling
        if time.time() >= deadline:
            return False
        time.sleep(poll_s)


RESTART_SETTLE_S = 2.0   # let the OS release COMFY_PORT after a kill before we rebind it


def restart_server(progress_cb: ProgressCb = None, ready_timeout_s: int = 120,
                   settle_s: float = RESTART_SETTLE_S) -> None:
    """Stop the current ComfyUI (ours, or whatever's on the port) and relaunch a fresh one
    with the required safe flags, blocking until it's responsive.

    Used by crash recovery: a TDR watchdog crash kills the process mid-render, so we bring it
    back. launch_server() always re-applies --disable-dynamic-vram (build_launch_command), so
    the relaunched server also fixes the crash's root cause. Raises ComfyError if the install
    can't be found or the server doesn't answer within ready_timeout_s.

    A short settle after the kill gives the OS time to release COMFY_PORT before the relaunch
    rebinds it - otherwise the new process loses the bind, exits, and we'd otherwise stall the
    full ready_timeout_s before reporting failure. We also watch the relaunched process so a
    bind failure (or any immediate exit) fails fast instead of waiting out that timeout.
    """
    _log(progress_cb, "comfy crash detected - restarting ComfyUI")
    try:
        stop_server()                 # kill our proc or whatever holds the port; ok if down
    except (ComfyError, subprocess.SubprocessError, OSError) as e:
        # A transient taskkill stall (subprocess.TimeoutExpired) or an OS-level kill error
        # must NOT abandon the whole local queue (L9): the relaunch below rebinds the port
        # regardless, and a truly stuck old process fails fast via the is_alive watch. Only
        # ComfyError was swallowed before, so a TimeoutExpired escaped and read as
        # 'restart failed'. Log and continue to the relaunch.
        _log(progress_cb, f"stop before restart failed (continuing to relaunch): {e}")
    if settle_s > 0:
        time.sleep(settle_s)          # let the OS release the port before we rebind it
    proc = launch_server()            # detached, with --disable-dynamic-vram --cache-none
    if not wait_until_responsive(ready_timeout_s, is_alive=lambda: proc.poll() is None):
        if proc.poll() is not None:   # exited without ever answering (likely a port-bind loss)
            raise ComfyError(f"ComfyUI exited immediately on restart (exit {proc.returncode}); "
                             "see data/comfyui_server.log.")
        raise ComfyError(f"ComfyUI did not come back up within {ready_timeout_s}s of restart.")
    _log(progress_cb, "ComfyUI restarted - retrying take")


def ensure_server(progress_cb: ProgressCb = None, ready_timeout_s: int = 120) -> bool:
    """Make sure a local ComfyUI is up before a render, launching it if it's down.

    Returns True if it was already running, False if we had to start it. Raises ComfyError
    if the install isn't found or the launched server never becomes responsive within
    ready_timeout_s. A server we launch always gets the required safe flags
    (--disable-dynamic-vram --cache-none via build_launch_command), so a server we cold-start
    is never the misconfigured kind. An *already-running* server is left as-is here; the
    dynamic-VRAM gate for that case stays with submit()'s preflight(), the right place for it.

    Called once before the crash-recovery loop (ui.main_window._make_runner) so a cold start
    is an honest "starting ComfyUI" step, not a server-down failure misread as a crash and
    laundered through the retry path. Distinct from restart_server: there's no live process to
    stop first, just a launch + wait (it reuses the same launch/wait building blocks).
    """
    if server_status()["running"]:
        return True
    _log(progress_cb, "ComfyUI is not running - starting it (this can take a minute)...")
    proc = launch_server()            # detached, with --disable-dynamic-vram --cache-none
    if not wait_until_responsive(ready_timeout_s, is_alive=lambda: proc.poll() is None):
        if proc.poll() is not None:   # exited without ever answering (likely a port-bind loss)
            raise ComfyError(f"ComfyUI exited immediately on launch (exit {proc.returncode}); "
                             "see data/comfyui_server.log.")
        raise ComfyError(f"ComfyUI did not become responsive within {ready_timeout_s}s of launch.")
    _log(progress_cb, "ComfyUI started - beginning render")
    return False


def monitor_snapshot(timeout: int = 2) -> dict:
    """One-shot gather of live ComfyUI state for the monitor window. Non-raising.

    Returns {"running": False} if the server is down, else running flag + version +
    python/pytorch/os + launch argv (settings) + dynamic_vram + ram/vram totals/free +
    a queue summary (running count, pending count, the running prompt id).
    """
    try:
        st = _api("/system_stats", timeout=timeout)
    except Exception:  # noqa: BLE001 - a monitor probe must never raise
        return {"running": False}
    system = st.get("system", {})
    dev = (st.get("devices") or [{}])[0]
    argv = system.get("argv", [])
    py = (system.get("python_version") or "").split(" ")[0] or None
    snap = {
        "running": True, "version": system.get("comfyui_version"),
        "python_version": py, "pytorch_version": system.get("pytorch_version"),
        "os": system.get("os"), "argv": argv, "dynamic_vram": dynamic_vram_enabled(argv),
        "async_offload": async_offload_enabled(argv, dev.get("type")),
        "ram_total": system.get("ram_total"), "ram_free": system.get("ram_free"),
        "device_name": dev.get("name"), "vram_total": dev.get("vram_total"),
        "vram_free": dev.get("vram_free"),
        "queue_running": 0, "queue_pending": 0, "running_prompt": None,
    }
    try:  # queue is best-effort - a failure here shouldn't blank the whole snapshot
        q = _api("/queue", timeout=timeout)
        running, pending = q.get("queue_running") or [], q.get("queue_pending") or []
        snap["queue_running"], snap["queue_pending"] = len(running), len(pending)
        if running and len(running[0]) > 1:  # item shape: [number, prompt_id, prompt, ...]
            snap["running_prompt"] = running[0][1]
    except Exception:  # noqa: BLE001
        pass
    return snap


def list_models(timeout: int = 10) -> dict:
    """Map of model-folder -> filenames via /models then /models/{folder}. Non-raising;
    returns {} if the server is down. Folders that error individually map to []."""
    try:
        folders = _api("/models", timeout=timeout)
    except Exception:  # noqa: BLE001
        return {}
    out: dict = {}
    for folder in folders:
        try:
            out[folder] = _api(f"/models/{folder}", timeout=timeout)
        except Exception:  # noqa: BLE001
            out[folder] = []
    return out


def _post(path: str, data: Optional[dict] = None, timeout: int = 5) -> None:
    """Fire-and-forget POST that tolerates an empty / non-JSON body (the control
    endpoints return a bare 200). Raises ComfyError on a transport/HTTP failure."""
    req = urllib.request.Request(COMFY_URL + path, method="POST")
    req.add_header("Content-Type", "application/json")
    req.data = json.dumps(data or {}).encode()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read()
    except urllib.error.URLError as e:  # includes HTTPError for 4xx/5xx
        raise ComfyError(f"ComfyUI POST {path} failed ({e}). Is it running?") from e


def stop_work(prompt_id: Optional[str] = None, timeout: int = 5) -> None:
    """Cancel current ComfyUI work without shutting the server down, then interrupt the
    running prompt. Raises ComfyError if the server is down.

    When `prompt_id` is given, only THAT prompt is dropped from the pending queue
    (POST /queue {delete: [id]}) - not the whole queue. Clearing the entire queue
    (`clear: True`) deleted foreign prompts (other tools sharing the server) AND left our
    own cleared prompt without a /history entry, so the take sat GENERATING for the full 1h
    timeout occupying the serialized local slot (L8). Falling back to `clear: True` only when
    we don't know the prompt id preserves the old blanket behaviour for callers that can't
    target (e.g. a manual Stop-work with nothing tracked).

    /interrupt is still global (ComfyUI has no per-prompt interrupt), but on our serialized
    local queue only our prompt is ever running, so interrupting stops just our render.
    """
    if prompt_id:
        _post("/queue", {"delete": [prompt_id]}, timeout=timeout)  # drop just our pending prompt
    else:
        _post("/queue", {"clear": True}, timeout=timeout)          # no id: drop everything pending
    _post("/interrupt", {}, timeout=timeout)                       # stop the one in progress


def stop_server(timeout: int = 10) -> None:
    """Shut the local ComfyUI down. Terminates the process we launched if we still have
    it; otherwise finds and kills whatever is listening on COMFY_PORT (so a server started
    by the CLI/script, or left over from a previous AnimGen run, can still be stopped).
    No-op if nothing is running. Raises ComfyError if a live server can't be located."""
    global _server_proc
    pid = _server_proc.pid if (_server_proc and _server_proc.poll() is None) else None
    if pid is None:
        pid = _pid_on_port(COMFY_PORT)
    if pid is None:
        _server_proc = None
        if server_status(timeout=2)["running"]:
            raise ComfyError("ComfyUI is running but its process could not be located "
                             "to stop it (try closing it from where it was launched).")
        return  # already down - nothing to do
    _kill_pid(pid, timeout=timeout)
    _server_proc = None


def _pid_on_port(port: int) -> Optional[int]:
    """PID of the process LISTENING on `port` (loopback), or None. Uses psutil if present,
    else parses netstat on Windows."""
    try:
        import psutil  # optional - not an AnimGen dependency, just a fast path
        for c in psutil.net_connections(kind="inet"):
            if c.laddr and c.laddr.port == port and c.status == psutil.CONN_LISTEN and c.pid:
                return c.pid
    except Exception:  # noqa: BLE001 - psutil missing or query failed; fall through
        pass
    if sys.platform != "win32":
        return None
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "tcp"], capture_output=True,
                             text=True, timeout=5).stdout
    except Exception:  # noqa: BLE001
        return None
    for line in out.splitlines():
        parts = line.split()  # e.g. ['TCP','127.0.0.1:8188','0.0.0.0:0','LISTENING','12345']
        if len(parts) >= 5 and parts[3].upper() == "LISTENING" and parts[1].endswith(f":{port}"):
            try:
                return int(parts[-1])
            except ValueError:
                return None
    return None


def _kill_pid(pid: int, timeout: int = 10) -> None:
    if sys.platform == "win32":
        r = subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            raise ComfyError(f"taskkill failed for PID {pid}: "
                             f"{(r.stderr or r.stdout).strip()}")
    else:
        import signal
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass


def copy_input_image(path: str | Path) -> str:
    """Copy a keypose into ComfyUI/input/ so LoadImage can read it. Returns basename.

    The destination is named by the file's CONTENT hash (kp_<sha1>.<ext>), not its source
    name. Every local keypose used to be called start.png/end.png, so distinct framings all
    contested the same ComfyUI/input/ slot and the only staleness guard was equal byte size -
    two framings that compressed to the same size would silently render the first one's image
    (M8). A content-derived name gives each distinct image its own stable slot: two renders of
    the same bytes reuse one file (cheap, correct dedupe), two different images never collide.
    """
    path = Path(path)
    if not path.exists():
        raise ComfyError(f"Keypose not found: {path}")
    data = path.read_bytes()
    digest = hashlib.sha1(data).hexdigest()
    ext = path.suffix.lower() or ".png"
    name = f"kp_{digest}{ext}"
    COMFY_INPUT_DIR.mkdir(parents=True, exist_ok=True)
    dest = COMFY_INPUT_DIR / name
    # Same content hash + same size => already the right bytes; only rewrite on a size mismatch
    # (a truncated/partial prior copy), which a hash collision would never otherwise reach.
    if not (dest.exists() and dest.stat().st_size == len(data)):
        dest.write_bytes(data)
    return name


def _nodes_by_class(wf: dict, class_type: str) -> list[str]:
    ids = [nid for nid, n in wf.items() if n.get("class_type") == class_type]
    return sorted(ids, key=lambda s: (len(s), s))  # numeric-ish ascending


def _disconnect_consumers(wf: dict, src_id: str) -> None:
    """Remove every input link that feeds FROM node `src_id`. Used to drop an FLF
    workflow's end-image conditioning when no end frame is supplied, so the Wan
    first-last node runs open-ended (I2V-style) instead of reusing a baked frame."""
    src = str(src_id)
    for node in wf.values():
        inputs = node.get("inputs")
        if not isinstance(inputs, dict):
            continue
        for field in [f for f, v in inputs.items()
                      if isinstance(v, list) and v and str(v[0]) == src]:
            del inputs[field]


# Input field names that carry end-image conditioning on an FLF video node
# (Wan's WanFirstLastFrameToVideo names it `end_image`). Their presence as a node
# *link* is the structural "this template is FLF-shaped" signal — robust to node ids
# and LoadImage count, unlike the old len(loads) > 1 heuristic.
_END_IMAGE_INPUT_FIELDS = frozenset({"end_image"})


def _has_end_image_conditioning(wf: dict) -> bool:
    """True if any node consumes an end-image link, i.e. the template is FLF-shaped."""
    for node in wf.values():
        inputs = node.get("inputs") if isinstance(node, dict) else None
        if not isinstance(inputs, dict):
            continue
        for field in _END_IMAGE_INPUT_FIELDS:
            v = inputs.get(field)
            if isinstance(v, list) and v:
                return True
    return False


# Core ComfyUI CLIP-loader node types that expose the optional `device` input
# (["default","cpu"]); the GGUF/custom loaders aren't guaranteed to, so we don't touch them.
_CLIP_LOADER_TYPES = frozenset({
    "CLIPLoader", "DualCLIPLoader", "TripleCLIPLoader", "QuadrupleCLIPLoader",
})


def _force_text_encoder_cpu(wf: dict) -> int:
    """Pin every CLIP-loader node to the CPU; return how many were switched.

    The text encoder (umt5-xxl here, ~6GB) runs once per render and doesn't need the GPU.
    On a 12GB card it otherwise competes with the diffusion model for VRAM - and ComfyUI
    keeps it partially resident across runs - so the 9GB Wan expert spills ~4GB to system
    RAM and streams it over PCIe every sampling step (the 36 s/it). Running the encoder on
    CPU keeps those 6GB out of VRAM entirely so the expert fits. Costs a one-time CPU encode
    per render (seconds); saves the per-step streaming. Only core CLIPLoader-family nodes
    expose the `device` input, so we set it only on those (setdefault-style, leaving an
    already-cpu node alone)."""
    switched = 0
    for node in wf.values():
        if isinstance(node, dict) and node.get("class_type") in _CLIP_LOADER_TYPES:
            node.setdefault("inputs", {})["device"] = "cpu"
            switched += 1
    return switched


def prepare_workflow(template: dict, *, start_img: Optional[str] = None,
                     end_img: Optional[str] = None, prompt: Optional[str] = None,
                     negative: Optional[str] = None, seed: Optional[int] = None,
                     node_roles: Optional[dict] = None,
                     sets: Optional[dict] = None,
                     text_encoder_cpu: bool = False) -> dict:
    """Return a mutated copy of `template` with our inputs applied.

    node_roles (from model_library 'comfy_nodes') may name: start_image, end_image,
    prompt, negative, seed_nodes[]. Missing roles fall back to heuristics.

    text_encoder_cpu pins CLIP-loader nodes to the CPU (see _force_text_encoder_cpu).
    """
    wf = copy.deepcopy(template)
    roles = node_roles or {}
    if text_encoder_cpu:
        _force_text_encoder_cpu(wf)

    loads = _nodes_by_class(wf, "LoadImage")
    clips = _nodes_by_class(wf, "CLIPTextEncode")

    def set_input(node_id: str, field: str, value) -> None:
        if node_id not in wf:
            raise ComfyError(f"node {node_id} not in workflow")
        wf[node_id].setdefault("inputs", {})[field] = value

    if start_img is not None:
        nid = roles.get("start_image") or (loads[0] if loads else None)
        if nid is None:
            raise ComfyError("no LoadImage node for start image")
        set_input(nid, "image", copy_input_image(start_img))
    # The end-image node: the declared end_image role, else a 2nd LoadImage. Resolved once
    # so setting the end frame and severing it for an open-ended render stay in lockstep.
    end_node = roles.get("end_image") or (loads[1] if len(loads) > 1 else None)
    if end_img is not None:
        if end_node is None:
            raise ComfyError("no second LoadImage node for end image")
        set_input(end_node, "image", copy_input_image(end_img))
    else:
        # No end frame -> run open-ended: sever the end-image conditioning so an FLF
        # workflow degrades to I2V instead of reusing the template's baked end frame.
        # Drive this off the declared end_image role (the authoritative FLF signal), not
        # a LoadImage-count heuristic: a single-LoadImage FLF template whose end frame is
        # fed by a non-LoadImage node would otherwise slip past len(loads) > 1 and keep its
        # baked end keyframe. If the workflow still carries end-image conditioning we can't
        # pin to a node, fail loudly rather than render against a stale baked frame
        # (mirrors the "no second LoadImage node for end image" guard above).
        if end_node is not None:
            _disconnect_consumers(wf, end_node)
        elif _has_end_image_conditioning(wf):
            raise ComfyError(
                "workflow has end-image conditioning but no end_image node could be "
                "identified for an open-ended render; declare comfy_nodes.end_image")
    if prompt is not None:
        nid = roles.get("prompt") or (clips[0] if clips else None)
        if nid is None:
            raise ComfyError("no CLIPTextEncode node for prompt")
        set_input(nid, "text", prompt)
    if negative is not None:
        nid = roles.get("negative") or (clips[1] if len(clips) > 1 else None)
        if nid is not None:
            set_input(nid, "text", negative)

    if seed is not None:
        seed_nodes = roles.get("seed_nodes")
        if not seed_nodes:
            seed_nodes = [nid for nid, n in wf.items()
                          if {"seed", "noise_seed"} & set(n.get("inputs", {}))]
        if not seed_nodes:
            raise ComfyError("--seed given but no seed/noise_seed field in workflow")
        for nid in seed_nodes:
            for field in ("seed", "noise_seed"):
                if field in wf[nid].get("inputs", {}):
                    wf[nid]["inputs"][field] = seed

    for target, value in (sets or {}).items():
        node_id, field = target.split(".", 1)
        cur = wf[node_id]["inputs"].get(field)
        if isinstance(cur, bool):
            value = str(value).lower() in ("1", "true", "yes")
        elif isinstance(cur, int):
            value = int(value)
        elif isinstance(cur, float):
            value = float(value)
        wf[node_id]["inputs"][field] = value

    return wf


def sampler_step_plan(wf: dict) -> dict:
    """{node_id: (global_offset, global_total)} for a multi-stage sampler chain, so a per-node
    step count can be reported against the run's TOTAL steps instead of restarting each stage.

    Wan 2.2 14B is a two-expert (high-/low-noise) model: its workflow splits the denoise across
    two KSamplerAdvanced nodes (e.g. steps 0->10 then 10->20). ComfyUI reports progress per node
    as value/own-steps, so without this map the bar fills 0..100% once per sampler and "counts to
    10 twice". With it, node A maps to steps 0..10 of 20 and node B to 10..20 of 20.

    Returns {} for a single-sampler workflow (nothing to remap -> the per-node value/max is already
    the whole run). Ordering is by start_at_step (the split-sampler chaining), node id breaking ties.
    Pure (no I/O); keys are str node ids to match the WS message ids.
    """
    stages = []
    for nid, node in (wf or {}).items():
        if not isinstance(node, dict) or "KSampler" not in str(node.get("class_type", "")):
            continue
        ins = node.get("inputs") or {}
        steps = ins.get("steps")
        if not isinstance(steps, int) or steps <= 0:
            continue
        start = ins.get("start_at_step")
        start = start if isinstance(start, int) and start > 0 else 0
        end = ins.get("end_at_step")
        end = min(end, steps) if isinstance(end, int) else steps
        run = max(0, end - start)
        if run > 0:
            stages.append((start, str(nid), run))
    if len(stages) < 2:
        return {}
    stages.sort()
    total = sum(r for _, _, r in stages)
    plan, off = {}, 0
    for _, nid, run in stages:
        plan[nid] = (off, total)
        off += run
    return plan


def progress_fraction(msg: dict, prompt_id: str, plan: Optional[dict] = None) -> tuple[Optional[float], str]:
    """(fraction 0..1, label) from a ComfyUI WS message, or (None, '') if it carries no
    usable progress for our prompt.

    Handles both the flat 'progress' message (value/max) and the newer per-node
    'progress_state', plus 'executing' with a null node (sampling done). Filters by
    prompt_id when the message carries one - local renders are serialized (one prompt at a
    time), so a legacy 'progress' with no prompt_id is still unambiguously ours.

    `plan` (from sampler_step_plan) remaps a per-node step count onto the run's TOTAL steps so a
    multi-stage sampler chain reports one continuous bar (e.g. "step 14/20") instead of restarting
    each stage. When the running node isn't in the plan the raw per-node value/max is used. Pure (no
    I/O) so it's unit-testable headless.
    """
    data = msg.get("data") or {}
    mpid = data.get("prompt_id")
    if mpid is not None and prompt_id and mpid != prompt_id:
        return None, ""              # progress for some other prompt

    def _frac(value, mx, nid=None) -> tuple[Optional[float], str]:
        if not (isinstance(value, (int, float)) and isinstance(mx, (int, float)) and mx > 0):
            return None, ""
        if plan and nid is not None and str(nid) in plan:
            off, total = plan[str(nid)]
            g = off + value
            return max(0.0, min(1.0, g / total)), f"step {int(g)}/{int(total)}"
        return max(0.0, min(1.0, value / mx)), f"step {int(value)}/{int(mx)}"

    mtype = msg.get("type")
    if mtype == "progress":
        return _frac(data.get("value"), data.get("max"), data.get("node"))
    if mtype == "progress_state":
        # Report only the furthest-along actively-running node. Deliberately DON'T infer
        # 100% from "all listed nodes finished": progress_state lists only nodes seen so far,
        # so between two samplers (sampler 1 done, sampler 2 not yet listed) that would flash
        # a premature 100%. The terminal 1.0 comes from the 'executing' null message below.
        running = [(nid, n) for nid, n in (data.get("nodes") or {}).items()
                   if isinstance(n, dict) and 0 < (n.get("value") or 0) < (n.get("max") or 0)]
        if running:
            nid, pick = max(running, key=lambda kv: (kv[1].get("value") or 0) / (kv[1].get("max") or 1))
            return _frac(pick.get("value"), pick.get("max"), nid)
        return None, ""
    if mtype == "executing" and data.get("node") is None:
        return 1.0, ""               # our prompt finished sampling
    return None, ""


def _ws_progress_listener(client_id: str, prompt_id: str, progress_cb: ProgressCb,
                          stop: "threading.Event", plan: Optional[dict] = None) -> None:
    """Best-effort: stream ComfyUI's WebSocket progress into progress_cb(frac=.., label=..).

    Any failure (no websocket-client, server without /ws, dropped socket) is swallowed - the
    /history poll in submit() still drives the render to completion, just without a live bar.
    """
    if progress_cb is None or os.environ.get("ANIMGEN_NO_WS_PROGRESS"):
        return
    try:
        import websocket  # websocket-client; optional dependency, best-effort
    except Exception:
        return
    ws_url = (COMFY_URL.replace("https://", "wss://").replace("http://", "ws://")
              + f"/ws?clientId={client_id}")
    ws = None
    # Telemetry: if this listener is implicated in a crash, these counts in animgen.log
    # right before the gap show whether it was flooded (e.g. huge/rapid binary preview
    # frames) - the kind of load that could blow a native stack. Cheap; logged every
    # _WS_TELEMETRY_S and once on exit.
    n_text = n_bin = bytes_total = max_frame = 0
    last_log = time.time()
    try:
        ws = websocket.create_connection(ws_url, timeout=5)
        ws.settimeout(1.0)
        _ws_logger.info("ws progress: connected prompt=%s", str(prompt_id)[:8])
        while not stop.is_set():
            try:
                raw = ws.recv()
            except websocket.WebSocketTimeoutException:
                continue             # tick so we can re-check stop
            except Exception:
                break
            size = len(raw) if raw is not None else 0
            bytes_total += size
            max_frame = max(max_frame, size)
            if not isinstance(raw, str) or not raw:
                n_bin += 1           # skip binary frames (preview images in practice)
            else:
                n_text += 1
                try:
                    msg = json.loads(raw)
                except (ValueError, TypeError):
                    msg = None
                if msg is not None:
                    frac, label = progress_fraction(msg, prompt_id, plan)
                    if frac is not None:
                        try:
                            progress_cb(frac=frac, label=label)
                        except Exception:
                            pass
            now = time.time()
            if now - last_log >= _WS_TELEMETRY_S:
                _ws_logger.info("ws progress: text=%d bin=%d bytes=%d maxframe=%d",
                                n_text, n_bin, bytes_total, max_frame)
                last_log = now
    except Exception as e:  # noqa: BLE001 - best-effort; never propagate
        _ws_logger.info("ws progress: listener error %r", e)
        return
    finally:
        _ws_logger.info("ws progress: done text=%d bin=%d bytes=%d maxframe=%d",
                        n_text, n_bin, bytes_total, max_frame)
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass


def _start_progress_ws(client_id: Optional[str], prompt_id: str,
                       progress_cb: ProgressCb, plan: Optional[dict] = None) -> "tuple[Optional[threading.Thread], Optional[threading.Event]]":
    """Spawn the best-effort WS progress listener; return (thread, stop_event).

    No-op (returns (None, None)) when there's no callback to feed or no client_id to
    subscribe with - ComfyUI routes progress to the socket holding the submitting
    client_id, so without it there's nothing to listen for. `plan` (sampler_step_plan) maps a
    multi-stage sampler chain onto one continuous step count.
    """
    if progress_cb is None or not client_id:
        return None, None
    if os.environ.get("ANIMGEN_NO_WS_PROGRESS"):   # bisection lever / escape hatch (rule #11)
        _ws_logger.info("ws progress: disabled via ANIMGEN_NO_WS_PROGRESS")
        return None, None
    stop = threading.Event()
    t = threading.Thread(target=_ws_progress_listener,
                         args=(client_id, prompt_id, progress_cb, stop, plan), daemon=True)
    t.start()
    return t, stop


def _stop_progress_ws(thread: "Optional[threading.Thread]",
                      stop: "Optional[threading.Event]") -> None:
    """Signal and join the listener started by _start_progress_ws (tolerates (None, None))."""
    if stop is not None:
        stop.set()
    if thread is not None:
        thread.join(timeout=2)


def _client_id_in_queue(queue: dict, prompt_id: str) -> Optional[str]:
    """Pure: the submitting client_id for `prompt_id` in a /queue payload, or None.

    A queue entry is [number, prompt_id, prompt, extra_data{client_id,...}, outputs]; the
    client_id rides in the extra_data dict. Scans both running and pending buckets. Split
    out from the I/O wrapper so it's unit-testable headless.
    """
    for bucket in ("queue_running", "queue_pending"):
        for entry in queue.get(bucket) or []:
            if isinstance(entry, list) and len(entry) > 1 and entry[1] == prompt_id:
                for part in entry[2:]:
                    if isinstance(part, dict) and part.get("client_id"):
                        return part["client_id"]
    return None


def _client_id_for_prompt(prompt_id: str, timeout: int = 5) -> Optional[str]:
    """The client_id that submitted `prompt_id`, read from the live /queue, or None.

    Orphan recovery re-attaches to a render whose submitting process is gone, so the
    original client_id (needed to resubscribe to its WS progress) survives only in the
    queue entry. Best-effort - never raises.
    """
    try:
        q = _api("/queue", timeout=timeout)
    except Exception:  # noqa: BLE001 - resubscription is best-effort
        return None
    return _client_id_in_queue(q, prompt_id)


def _entry_outputs(entry: dict) -> list[Path]:
    """Absolute paths of every media file a /history entry produced."""
    produced = []
    for out in entry.get("outputs", {}).values():
        for key in ("images", "video", "videos", "gifs"):
            for item in out.get(key, []):
                sub = item.get("subfolder", "")
                produced.append(COMFY_OUTPUT_DIR / sub / item["filename"])
    return produced


def _claim_output(produced: list[Path], out_path: Path) -> dict:
    """Copy the last produced file to out_path and return the take-result dict.

    Raises ComfyError if `produced` is empty: a /history entry can report outputs under
    only unrecognized keys (none of images/video/videos/gifs), which would otherwise yield a
    DONE take pointing at an out_path that was never written (L10). No file = not a success.
    """
    if not produced:
        raise ComfyError("ComfyUI reported outputs but none were a recognized media file "
                         "(images/video/videos/gifs) - nothing to claim.")
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(produced[-1], out_path)
    return {"video_path": str(out_path), "produced": [str(p) for p in produced]}


# A single failed /history poll is NOT fatal: a busy ComfyUI can block its HTTP thread past
# the socket timeout while loading 14B weights, and a 1h render polls ~720 times. Tolerate a
# short run of consecutive poll failures (with a brief backoff) before giving up, so one
# transient stall doesn't raise out of the runner and get misread as a crash / lose an hour
# of GPU work. A poll that SUCCEEDS resets the streak; only a sustained outage is fatal.
_POLL_MAX_CONSECUTIVE_FAILURES = 6
_POLL_FAILURE_BACKOFF_S = 5


def _poll_until_done(pid: str, out_path: Path, progress_cb: ProgressCb,
                     timeout_s: int, poll_s: int) -> dict:
    """Poll /history/{pid} until the prompt errors, finishes, or times out.

    Shared by submit() (which queues first) and monitor() (which re-attaches to a
    prompt some earlier, now-dead worker queued). Raises ComfyError on failure.

    A transient poll failure (server briefly unreachable / a read timeout while it loads
    weights) is tolerated: it's retried with a short backoff up to
    _POLL_MAX_CONSECUTIVE_FAILURES in a row before the last error is re-raised. This keeps a
    momentary HTTP stall from failing the whole render (M4).
    """
    t0 = time.time()
    fails = 0
    while True:
        time.sleep(poll_s)
        try:
            hist = _api(f"/history/{pid}")
            fails = 0
        except ComfyError as e:
            fails += 1
            if fails >= _POLL_MAX_CONSECUTIVE_FAILURES:
                raise ComfyError(f"lost contact with ComfyUI while polling {pid} "
                                 f"({fails} consecutive failures): {e}") from e
            _log(progress_cb, f"poll stalled ({fails}/{_POLL_MAX_CONSECUTIVE_FAILURES}), "
                              f"retrying: {e}")
            if time.time() - t0 > timeout_s:
                raise ComfyError("timed out after 1h") from e
            time.sleep(_POLL_FAILURE_BACKOFF_S)
            continue
        if pid in hist:
            entry = hist[pid]
            status = entry.get("status", {})
            if status.get("status_str") == "error":
                msgs = status.get("messages", [])
                detail = next((json.dumps(m[1])[:1500] for m in msgs
                               if m[0] == "execution_error"), "")
                raise ComfyError(f"workflow error: {detail}")
            if status.get("completed") and not entry.get("outputs"):
                raise ComfyError("completed with NO outputs (full cache hit?) - "
                                 "change seed/prompt so something renders")
            if entry.get("outputs"):
                _log(progress_cb, f"done in {int(time.time() - t0)}s")
                return _claim_output(_entry_outputs(entry), out_path)
        if time.time() - t0 > timeout_s:
            raise ComfyError("timed out after 1h")


def submit(wf: dict, out_path: Path, progress_cb: ProgressCb = None,
           timeout_s: int = 3600, poll_s: int = 5,
           on_submit: Optional[Callable[[str], None]] = None) -> dict:
    preflight(progress_cb)
    client_id = uuid.uuid4().hex
    res = _api("/prompt", {"prompt": wf, "client_id": client_id})
    pid = res["prompt_id"]
    _log(progress_cb, f"queued {pid}")
    if on_submit:                     # record the prompt id NOW so a take orphaned mid-render
        on_submit(pid)                # (app restart) can be reconciled against the backend
    # map the workflow's sampler chain onto one continuous step count (Wan's 2 experts -> 0..20,
    # not 0..10 twice); {} for single-sampler workflows leaves the raw per-node % unchanged.
    ws_thread, ws_stop = _start_progress_ws(client_id, pid, progress_cb,
                                            sampler_step_plan(wf))  # live step % (best-effort)
    try:
        return _poll_until_done(pid, out_path, progress_cb, timeout_s, poll_s)
    finally:
        _stop_progress_ws(ws_thread, ws_stop)


def monitor(prompt_id: str, out_path: Path, progress_cb: ProgressCb = None,
            timeout_s: int = 3600, poll_s: int = 5) -> dict:
    """Re-attach to an already-queued prompt and collect its output when it finishes.

    Used by orphan recovery: a take that was rendering when the app died still has its
    prompt running/queued on the (separate, surviving) ComfyUI process. No preflight or
    /prompt POST here - the work is already in flight; we only poll and claim the file.

    The submitting process is gone, so we recover its client_id from the live /queue to
    resubscribe to the WS step-% (same listener submit() uses); if it can't be found - the
    prompt already finished, or the queue dropped it - we just poll, as before.
    """
    _log(progress_cb, f"re-attached to {prompt_id}")
    client_id = _client_id_for_prompt(prompt_id)
    ws_thread, ws_stop = _start_progress_ws(client_id, prompt_id, progress_cb)
    try:
        return _poll_until_done(prompt_id, out_path, progress_cb, timeout_s, poll_s)
    finally:
        _stop_progress_ws(ws_thread, ws_stop)


def _seeds_in_workflow(wf) -> set:
    """Every concrete seed/noise_seed baked into a workflow's nodes."""
    seeds = set()
    if isinstance(wf, dict):
        for node in wf.values():
            ins = node.get("inputs", {}) if isinstance(node, dict) else {}
            for f in ("seed", "noise_seed"):
                if isinstance(ins.get(f), int):
                    seeds.add(ins[f])
    return seeds


def _prompt_workflow(prompt_field) -> dict:
    """Pull the node-graph dict out of a /history or /queue 'prompt' field.

    Comfy stores it as a list whose elements vary by endpoint; the graph is the dict
    whose values look like nodes (have 'class_type' / 'inputs')."""
    if isinstance(prompt_field, dict):
        return prompt_field
    if isinstance(prompt_field, list):
        for el in prompt_field:
            if isinstance(el, dict) and any(
                    isinstance(v, dict) and ("class_type" in v or "inputs" in v)
                    for v in el.values()):
                return el
    return {}


def history_view(timeout: int = 5) -> list[dict]:
    """Normalized /history: [{prompt_id, seeds, outputs, ok}], oldest-first."""
    hist = _api("/history", timeout=timeout)
    out = []
    for pid, entry in hist.items():
        status = entry.get("status", {})
        out.append({
            "prompt_id": pid,
            "seeds": _seeds_in_workflow(_prompt_workflow(entry.get("prompt"))),
            "outputs": _entry_outputs(entry),
            "ok": status.get("status_str") != "error",
        })
    return out


def queue_view(timeout: int = 5) -> list[dict]:
    """Normalized /queue: [{prompt_id, seeds, state}] for running + pending prompts."""
    q = _api("/queue", timeout=timeout)
    out = []
    for state, key in (("running", "queue_running"), ("pending", "queue_pending")):
        for item in q.get(key, []):
            pid = item[1] if len(item) > 1 else None
            wf = item[2] if len(item) > 2 else None
            out.append({"prompt_id": pid, "seeds": _seeds_in_workflow(wf), "state": state})
    return out


def generate(template_path: str | Path, out_path: Path, *, start: Optional[str] = None,
             end: Optional[str] = None, prompt: Optional[str] = None,
             negative: Optional[str] = None, seed: Optional[int] = None,
             node_roles: Optional[dict] = None, sets: Optional[dict] = None,
             progress_cb: ProgressCb = None, dry_run: bool = False,
             on_submit: Optional[Callable[[str], None]] = None,
             text_encoder_cpu: bool = False) -> dict:
    template = json.loads(Path(template_path).read_text(encoding="utf-8"))
    wf = prepare_workflow(template, start_img=start, end_img=end, prompt=prompt,
                          negative=negative, seed=seed, node_roles=node_roles, sets=sets,
                          text_encoder_cpu=text_encoder_cpu)
    if dry_run:
        return {"dry_run": True, "workflow": wf}
    return submit(wf, Path(out_path), progress_cb, on_submit=on_submit)
