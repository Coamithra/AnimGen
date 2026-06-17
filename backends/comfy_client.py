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
import json
import os
import shutil
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


class ComfyError(RuntimeError):
    pass


def _log(cb: ProgressCb, msg: str) -> None:
    if cb:
        cb(msg)


def _api(path: str, data=None, timeout: int = 30) -> dict:
    req = urllib.request.Request(COMFY_URL + path)
    if data is not None:
        req.add_header("Content-Type", "application/json")
        req.data = json.dumps(data).encode()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.URLError as e:  # type: ignore[name-defined]
        raise ComfyError(f"ComfyUI unreachable at {COMFY_URL} ({e}). Is it running?") from e


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


def preflight(progress_cb: ProgressCb = None) -> None:
    """Abort before launching a local job if the running ComfyUI has dynamic VRAM on.

    Queries /system_stats (which echoes the server's launch argv) and raises ComfyError
    with the fix if dynamic VRAM is enabled. No-op when ANIMGEN_ALLOW_DYNAMIC_VRAM is set.
    Doubles as a reachability check (clear error if the server is down).
    """
    if os.environ.get("ANIMGEN_ALLOW_DYNAMIC_VRAM"):
        _log(progress_cb, "preflight: dynamic-VRAM guard bypassed (ANIMGEN_ALLOW_DYNAMIC_VRAM)")
        return
    stats = _api("/system_stats")
    argv = stats.get("system", {}).get("argv", [])
    if dynamic_vram_enabled(argv):
        raise ComfyError(
            "ComfyUI is running with DYNAMIC VRAM ENABLED. On the 12GB card this trips "
            "Windows' 2s GPU watchdog (TDR) on 14B renders and kills the server mid-job. "
            "Restart ComfyUI with --disable-dynamic-vram - e.g. run "
            "scripts/launch_comfyui.py (or .bat), or add the flag to your own launch "
            "command. Set ANIMGEN_ALLOW_DYNAMIC_VRAM=1 to bypass this guard. Details: "
            "../Fighter/research/comfyui-gpu-watchdog-crash-and-aimdo.md."
        )
    _log(progress_cb, "preflight: dynamic VRAM disabled - OK")


# --- Server process management ----------------------------------------------
# AnimGen can start the local ComfyUI itself (the "Launch ComfyUI" button / the
# scripts/launch_comfyui.py CLI) so --disable-dynamic-vram is always applied.
# build_launch_command() is the single source of truth for HOW to start it.

REQUIRED_FLAGS = ["--disable-dynamic-vram"]
_DEFAULT_FLAGS = [("--listen", "127.0.0.1"), ("--port", str(COMFY_PORT))]  # (flag, value)
_server_proc: "Optional[subprocess.Popen]" = None  # the ComfyUI we launched, if any


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

    Returns {running, version, dynamic_vram, argv}. dynamic_vram is None when the server
    is down, else True/False derived from its launch argv (the gate preflight enforces).
    """
    try:
        system = _api("/system_stats", timeout=timeout).get("system", {})
    except Exception:  # noqa: BLE001 - a status probe must never raise
        return {"running": False, "version": None, "dynamic_vram": None, "argv": []}
    argv = system.get("argv", [])
    return {"running": True, "version": system.get("comfyui_version"),
            "dynamic_vram": dynamic_vram_enabled(argv), "argv": argv}


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
                                creationflags=flags, close_fds=True)
    finally:
        logfile.close()  # the child keeps its own inherited handle
    _server_proc = proc  # remembered so stop_server() can terminate exactly this one
    return proc


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


def stop_work(timeout: int = 5) -> None:
    """Cancel current ComfyUI work without shutting the server down: wipe the pending
    queue, then interrupt the running prompt. Raises ComfyError if the server is down."""
    _post("/queue", {"clear": True}, timeout=timeout)   # drop anything not yet started
    _post("/interrupt", {}, timeout=timeout)            # stop the one in progress


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
    """Copy a keypose into ComfyUI/input/ so LoadImage can read it. Returns basename."""
    path = Path(path)
    if not path.exists():
        raise ComfyError(f"Keypose not found: {path}")
    COMFY_INPUT_DIR.mkdir(parents=True, exist_ok=True)
    dest = COMFY_INPUT_DIR / path.name
    if not (dest.exists() and dest.stat().st_size == path.stat().st_size):
        shutil.copy2(path, dest)
    return path.name


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


def prepare_workflow(template: dict, *, start_img: Optional[str] = None,
                     end_img: Optional[str] = None, prompt: Optional[str] = None,
                     negative: Optional[str] = None, seed: Optional[int] = None,
                     node_roles: Optional[dict] = None,
                     sets: Optional[dict] = None) -> dict:
    """Return a mutated copy of `template` with our inputs applied.

    node_roles (from model_library 'comfy_nodes') may name: start_image, end_image,
    prompt, negative, seed_nodes[]. Missing roles fall back to heuristics.
    """
    wf = copy.deepcopy(template)
    roles = node_roles or {}

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
    if end_img is not None:
        nid = roles.get("end_image") or (loads[1] if len(loads) > 1 else None)
        if nid is None:
            raise ComfyError("no second LoadImage node for end image")
        set_input(nid, "image", copy_input_image(end_img))
    elif roles.get("end_image") or len(loads) > 1:
        # No end frame -> run open-ended: sever the end-image conditioning so an FLF
        # workflow degrades to I2V instead of reusing the template's baked end frame.
        _disconnect_consumers(wf, roles.get("end_image") or loads[1])
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


def progress_fraction(msg: dict, prompt_id: str) -> tuple[Optional[float], str]:
    """(fraction 0..1, label) from a ComfyUI WS message, or (None, '') if it carries no
    usable progress for our prompt.

    Handles both the flat 'progress' message (value/max) and the newer per-node
    'progress_state', plus 'executing' with a null node (sampling done). Filters by
    prompt_id when the message carries one - local renders are serialized (one prompt at a
    time), so a legacy 'progress' with no prompt_id is still unambiguously ours. Pure (no
    I/O) so it's unit-testable headless.
    """
    data = msg.get("data") or {}
    mpid = data.get("prompt_id")
    if mpid is not None and prompt_id and mpid != prompt_id:
        return None, ""              # progress for some other prompt

    def _frac(value, mx) -> tuple[Optional[float], str]:
        if isinstance(value, (int, float)) and isinstance(mx, (int, float)) and mx > 0:
            return max(0.0, min(1.0, value / mx)), f"step {int(value)}/{int(mx)}"
        return None, ""

    mtype = msg.get("type")
    if mtype == "progress":
        return _frac(data.get("value"), data.get("max"))
    if mtype == "progress_state":
        nodes = [n for n in (data.get("nodes") or {}).values() if isinstance(n, dict)]
        running = [n for n in nodes
                   if 0 < (n.get("value") or 0) < (n.get("max") or 0)]
        if running:                  # report the furthest-along actively-running node
            pick = max(running, key=lambda n: (n.get("value") or 0) / (n.get("max") or 1))
            return _frac(pick.get("value"), pick.get("max"))
        maxes = [(n.get("value") or 0, n.get("max") or 0) for n in nodes]
        if maxes and all(m > 0 and v >= m for v, m in maxes):
            return 1.0, ""           # every node finished
        return None, ""
    if mtype == "executing" and data.get("node") is None:
        return 1.0, ""               # our prompt finished sampling
    return None, ""


def _ws_progress_listener(client_id: str, prompt_id: str, progress_cb: ProgressCb,
                          stop: "threading.Event") -> None:
    """Best-effort: stream ComfyUI's WebSocket progress into progress_cb(frac=.., label=..).

    Any failure (no websocket-client, server without /ws, dropped socket) is swallowed - the
    /history poll in submit() still drives the render to completion, just without a live bar.
    """
    if progress_cb is None:
        return
    try:
        import websocket  # websocket-client; optional dependency, best-effort
    except Exception:
        return
    ws_url = (COMFY_URL.replace("https://", "wss://").replace("http://", "ws://")
              + f"/ws?clientId={client_id}")
    ws = None
    try:
        ws = websocket.create_connection(ws_url, timeout=5)
        ws.settimeout(1.0)
        while not stop.is_set():
            try:
                raw = ws.recv()
            except websocket.WebSocketTimeoutException:
                continue             # tick so we can re-check stop
            except Exception:
                break
            if not isinstance(raw, str) or not raw:
                continue             # binary frames are preview images - skip
            try:
                msg = json.loads(raw)
            except (ValueError, TypeError):
                continue
            frac, label = progress_fraction(msg, prompt_id)
            if frac is not None:
                try:
                    progress_cb(frac=frac, label=label)
                except Exception:
                    pass
    except Exception:
        return
    finally:
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass


def submit(wf: dict, out_path: Path, progress_cb: ProgressCb = None,
           timeout_s: int = 3600, poll_s: int = 5) -> dict:
    preflight(progress_cb)
    client_id = uuid.uuid4().hex
    res = _api("/prompt", {"prompt": wf, "client_id": client_id})
    pid = res["prompt_id"]
    _log(progress_cb, f"queued {pid}")
    stop = threading.Event()
    ws_thread: Optional[threading.Thread] = None
    if progress_cb is not None:      # listen for live step progress over the WS (best-effort)
        ws_thread = threading.Thread(target=_ws_progress_listener,
                                     args=(client_id, pid, progress_cb, stop), daemon=True)
        ws_thread.start()
    t0 = time.time()
    try:
        while True:
            time.sleep(poll_s)
            hist = _api(f"/history/{pid}")
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
                    produced = []
                    for out in entry["outputs"].values():
                        for key in ("images", "video", "videos", "gifs"):
                            for item in out.get(key, []):
                                sub = item.get("subfolder", "")
                                produced.append(COMFY_OUTPUT_DIR / sub / item["filename"])
                    _log(progress_cb, f"done in {int(time.time() - t0)}s")
                    if produced:
                        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(produced[-1], out_path)
                    return {"video_path": str(out_path), "produced": [str(p) for p in produced]}
            if time.time() - t0 > timeout_s:
                raise ComfyError("timed out after 1h")
    finally:
        stop.set()
        if ws_thread is not None:
            ws_thread.join(timeout=2)


def generate(template_path: str | Path, out_path: Path, *, start: Optional[str] = None,
             end: Optional[str] = None, prompt: Optional[str] = None,
             negative: Optional[str] = None, seed: Optional[int] = None,
             node_roles: Optional[dict] = None, sets: Optional[dict] = None,
             progress_cb: ProgressCb = None, dry_run: bool = False) -> dict:
    template = json.loads(Path(template_path).read_text(encoding="utf-8"))
    wf = prepare_workflow(template, start_img=start, end_img=end, prompt=prompt,
                          negative=negative, seed=seed, node_roles=node_roles, sets=sets)
    if dry_run:
        return {"dry_run": True, "workflow": wf}
    return submit(wf, Path(out_path), progress_cb)
