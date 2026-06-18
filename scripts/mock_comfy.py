"""Fake ComfyUI server for offline, GPU-free stress testing of the AnimGen app.

WHY: the overnight Biker batch keeps dying. Three nvlddmkm Event 153 GPU-driver faults
(2026-06-18 10:23 / 12:27 / 16:27) each killed ComfyUI *and* AnimGen together. We want to
know whether AnimGen dies because of the GPU/real-ComfyUI, or whether it has an independent
failure (the rule #18 paint-crash / a leak / a process-lifecycle issue). So: stand up a fake
ComfyUI that speaks just enough of the real protocol for the *unmodified* app to drive real
renders through it, with ZERO GPU involved. Run a big batch for a few hours:

  - app SURVIVES  -> the deaths were the GPU / real ComfyUI (TdrDelay fix is the right track)
  - app DIES anyway -> an app-intrinsic bug (rule #18 QProgressBar pile-up, leak, timeout)

It also reproduces the rule #18 Queue-tab accumulation (one QProgressBar per PENDING take)
with no GPU, since those bars are created per pending row regardless of the backend.

PROTOCOL (matches backends/comfy_client.py):
  GET  /system_stats     -> safe argv (so preflight passes) + a cuda device
  POST /prompt           -> {"prompt_id": ...}; starts a fake timed "render"
  GET  /history/{pid}    -> {} until the fake delay elapses, then completed + outputs that
                            reference a real canned .mp4 under <comfy>/output/ (the app copies
                            it to the take path and extracts frames -- real CPU load). With
                            --fail-rate, a fraction of renders instead return a status_str=="error"
                            entry (an execution_error message, no outputs) so comfy_client raises
                            ComfyError and the app records a FAILED take -- the server stays UP, so
                            it reads as a genuine workflow error, exercising crash-recovery's
                            "up == not a crash" discrimination and the restart-take path offline
  GET  /queue            -> running/pending buckets (benign; used by orphan recovery)
  POST /interrupt, /queue(clear), GET /object_info, GET /prompt -> benign
  GET  /ws?clientId=...  -> hand-rolled WebSocket streaming `progress` frames (+ optional
                            binary preview frames) then `executing`(null), like real ComfyUI

Run (keep the REAL ComfyUI shut down so port 8188 is free for the mock):
  PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe scripts/mock_comfy.py
  PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe scripts/mock_comfy.py --delay 8 --jitter 3
  PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe scripts/mock_comfy.py --fail-rate 0.25

Then launch the app normally and fire a big "Generate batch...". This is AnimGen's supported
offline GPU-free test/repro harness -- see "Offline GPU-free harness (mock ComfyUI)" in
CLAUDE.md.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import random
import shutil
import struct
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
COMFY_DIR = Path(os.environ.get("ANIMGEN_COMFY_DIR") or (REPO_ROOT.parent / "comfyui"))
OUTPUT_DIR = COMFY_DIR / "output"
MOCK_SUBFOLDER = "animgen_mock"
MOCK_FILENAME = "mock.mp4"

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

# Tunables (CLI overrides below)
RENDER_S = 10.0           # fake per-render wall time (the local queue is serialized)
JITTER_S = 3.0            # +/- random jitter so takes vary a touch
WS_PREVIEW_BYTES = 3000   # binary preview-frame size to mirror real ~3584 maxframe; 0 disables
FAIL_RATE = 0.0           # fraction (0..1) of renders that return a workflow error (FAILED take)
SAFE_ARGV = ["main.py", "--listen", "127.0.0.1", "--port", "8188",
             "--disable-dynamic-vram", "--disable-async-offload", "--cache-none"]

_lock = threading.Lock()
_renders: dict[str, dict] = {}   # pid -> {created, ready_at, done, fail}
_order: list[str] = []           # submission order (serialized) for WS active-pid lookup
_counter = 0
_stats = {"submitted": 0, "completed": 0, "failed": 0, "ws_open": 0, "started": time.time()}


def _canned_output() -> Path:
    """Make sure a real, decodable .mp4 exists at the path /history will advertise."""
    dest = OUTPUT_DIR / MOCK_SUBFOLDER / MOCK_FILENAME
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    takes = sorted((REPO_ROOT / "data").glob("*.assets/takes/*.mp4"),
                   key=lambda p: p.stat().st_size)
    if takes:
        shutil.copy2(takes[0], dest)
        return dest
    # fallback: synthesize a tiny clip with PyAV
    try:
        import av  # noqa
        import numpy as np
        container = av.open(str(dest), mode="w")
        stream = container.add_stream("libx264", rate=8)
        stream.width, stream.height, stream.pix_fmt = 64, 64, "yuv420p"
        for i in range(8):
            arr = (np.zeros((64, 64, 3), dtype="uint8") + (i * 30) % 255)
            frame = av.VideoFrame.from_ndarray(arr, format="rgb24")
            for pkt in stream.encode(frame):
                container.mux(pkt)
        for pkt in stream.encode():
            container.mux(pkt)
        container.close()
    except Exception as e:  # noqa: BLE001
        raise SystemExit(f"no canned take to serve and PyAV synth failed: {e}")
    return dest


def _ws_frame(payload: bytes, opcode: int) -> bytes:
    """Encode one server->client WebSocket frame (FIN set, unmasked)."""
    header = bytearray([0x80 | opcode])
    n = len(payload)
    if n < 126:
        header.append(n)
    elif n < 65536:
        header.append(126)
        header += struct.pack(">H", n)
    else:
        header.append(127)
        header += struct.pack(">Q", n)
    return bytes(header) + payload


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # silence per-request noise; we log our own events
        pass

    # ---- helpers ---------------------------------------------------------
    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _read_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception:  # noqa: BLE001
            return {}

    # ---- routing ---------------------------------------------------------
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/ws":
            return self._serve_ws()
        if path == "/system_stats":
            return self._json({
                "system": {"argv": SAFE_ARGV, "comfyui_version": "mock-0.1",
                           "os": "nt", "python_version": "3.12", "ram_total": 1, "ram_free": 1},
                "devices": [{"name": "Mock RTX (no GPU)", "type": "cuda", "index": 0,
                             "vram_total": 12884901888, "vram_free": 8000000000,
                             "torch_vram_total": 0, "torch_vram_free": 0}],
            })
        if path.startswith("/history"):
            pid = path[len("/history/"):].strip("/")
            return self._json(self._history(pid))
        if path == "/queue":
            return self._json(self._queue())
        if path == "/prompt":
            with _lock:
                remaining = sum(1 for r in _renders.values() if not r["done"])
            return self._json({"exec_info": {"queue_remaining": remaining}})
        if path == "/object_info":
            return self._json({})
        return self._json({})   # benign catch-all so comfy_client._api never raises

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        body = self._read_body()
        if path == "/prompt":
            return self._json(self._submit(body))
        if path in ("/interrupt", "/queue", "/free", "/upload/image"):
            return self._json({})
        return self._json({})

    # ---- behaviour -------------------------------------------------------
    def _submit(self, body):
        global _counter
        delay = max(0.5, RENDER_S + random.uniform(-JITTER_S, JITTER_S))
        pid = hashlib.md5(f"{time.time()}-{random.random()}".encode()).hexdigest()
        fail = random.random() < FAIL_RATE
        now = time.time()
        with _lock:
            _counter += 1
            number = _counter
            _renders[pid] = {"created": now, "ready_at": now + delay, "done": False,
                             "fail": fail}
            _order.append(pid)
            _stats["submitted"] += 1
            sub = _stats["submitted"]
        print(f"[mock] submit #{number} pid={pid[:8]} delay={delay:0.1f}s"
              f"{' FAIL' if fail else ''} (submitted={sub})", flush=True)
        return {"prompt_id": pid, "number": number, "node_errors": {}}

    def _history(self, pid):
        if not pid:
            return {}
        with _lock:
            r = _renders.get(pid)
            if r is None:
                return {}
            if not r["done"] and time.time() >= r["ready_at"]:
                r["done"] = True
                if r.get("fail"):
                    _stats["failed"] += 1
                    print(f"[mock] FAIL   pid={pid[:8]} (failed={_stats['failed']})", flush=True)
                else:
                    _stats["completed"] += 1
                    print(f"[mock] done   pid={pid[:8]} (completed={_stats['completed']})",
                          flush=True)
            if not r["done"]:
                return {}
            failed = r.get("fail", False)
        if failed:
            return {pid: {
                "status": {"status_str": "error", "completed": False, "messages": [
                    ["execution_start", {"prompt_id": pid}],
                    ["execution_error", {"prompt_id": pid, "node_id": "sampler",
                                         "node_type": "KSampler",
                                         "exception_type": "MockInjectedFailure",
                                         "exception_message":
                                             "mock --fail-rate injected workflow error"}]]},
                "outputs": {},
            }}
        return {pid: {
            "status": {"status_str": "success", "completed": True, "messages": [
                ["execution_start", {"prompt_id": pid}],
                ["execution_success", {"prompt_id": pid}]]},
            "outputs": {"out": {"video": [{"filename": MOCK_FILENAME,
                                           "subfolder": MOCK_SUBFOLDER, "type": "output"}]}},
        }}

    def _queue(self):
        with _lock:
            running = [[1, p, {}, {"client_id": "mock"}, {}]
                       for p in _order if not _renders.get(p, {}).get("done", True)][:1]
        return {"queue_running": running, "queue_pending": []}

    # ---- minimal WebSocket: stream progress for the active render --------
    def _serve_ws(self):
        key = self.headers.get("Sec-WebSocket-Key")
        if not key or "upgrade" not in (self.headers.get("Connection", "").lower()):
            return self._json({})           # not a real WS handshake
        accept = base64.b64encode(
            hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
        try:
            self.send_response(101, "Switching Protocols")
            self.send_header("Upgrade", "websocket")
            self.send_header("Connection", "Upgrade")
            self.send_header("Sec-WebSocket-Accept", accept)
            self.end_headers()
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            return
        with _lock:
            _stats["ws_open"] += 1
        sock = self.connection
        try:
            sock.settimeout(0.5)
            self._stream_progress(sock)
        except Exception:  # noqa: BLE001 - best-effort, mirror real WS flakiness
            pass
        finally:
            with _lock:
                _stats["ws_open"] -= 1
            self.close_connection = True

    def _stream_progress(self, sock):
        # Find the active (latest not-done) render; stream value/max 0..N over its remaining
        # time, then executing(null). Optionally send binary preview frames to mirror the real
        # WS load that rule #18 telemetry flagged.
        with _lock:
            pid = next((p for p in reversed(_order)
                        if not _renders.get(p, {}).get("done", True)), None)
            r = _renders.get(pid) if pid else None
        if not pid or not r:
            time.sleep(0.3)
            return
        steps = 20
        remaining = max(0.2, r["ready_at"] - time.time())
        per = remaining / steps
        preview = os.urandom(WS_PREVIEW_BYTES) if WS_PREVIEW_BYTES > 0 else b""
        for i in range(1, steps + 1):
            with _lock:
                if _renders.get(pid, {}).get("done", True):
                    break
            msg = {"type": "progress",
                   "data": {"value": i, "max": steps, "prompt_id": pid, "node": "sampler"}}
            try:
                if preview:
                    sock.sendall(_ws_frame(preview, 0x2))   # binary preview frame
                sock.sendall(_ws_frame(json.dumps(msg).encode(), 0x1))
            except (BrokenPipeError, ConnectionResetError, OSError):
                return
            time.sleep(per)
        try:
            done = {"type": "executing", "data": {"node": None, "prompt_id": pid}}
            sock.sendall(_ws_frame(json.dumps(done).encode(), 0x1))
        except (BrokenPipeError, ConnectionResetError, OSError):
            return
        # keep the socket open briefly so the client's poll wins the "done" race, like real comfy
        end = time.time() + 30
        while time.time() < end:
            try:
                if not sock.recv(256):   # client closed
                    return
            except (TimeoutError, OSError):
                continue


def _heartbeat(port):
    while True:
        time.sleep(60)
        with _lock:
            up = time.time() - _stats["started"]
            print(f"[mock] heartbeat up={up/60:0.1f}min submitted={_stats['submitted']} "
                  f"completed={_stats['completed']} failed={_stats['failed']} "
                  f"ws_open={_stats['ws_open']} "
                  f"inflight={sum(1 for r in _renders.values() if not r['done'])}", flush=True)


def main(argv):
    global RENDER_S, JITTER_S, WS_PREVIEW_BYTES, FAIL_RATE
    ap = argparse.ArgumentParser(description="Fake ComfyUI for GPU-free AnimGen stress testing")
    ap.add_argument("--port", type=int, default=8188)
    ap.add_argument("--delay", type=float, default=RENDER_S, help="fake per-render seconds")
    ap.add_argument("--jitter", type=float, default=JITTER_S, help="+/- render jitter seconds")
    ap.add_argument("--preview-bytes", type=int, default=WS_PREVIEW_BYTES,
                    help="binary WS preview-frame size (0 disables)")
    ap.add_argument("--fail-rate", type=float, default=FAIL_RATE,
                    help="fraction (0..1) of renders that return a workflow error (FAILED take)")
    args = ap.parse_args(argv)
    RENDER_S, JITTER_S, WS_PREVIEW_BYTES = args.delay, args.jitter, args.preview_bytes
    FAIL_RATE = min(1.0, max(0.0, args.fail_rate))

    canned = _canned_output()
    print(f"[mock] canned output: {canned} ({canned.stat().st_size} bytes)", flush=True)
    print(f"[mock] COMFY output dir: {OUTPUT_DIR}", flush=True)
    print(f"[mock] render={RENDER_S}s +/-{JITTER_S}s  preview_bytes={WS_PREVIEW_BYTES}  "
          f"fail_rate={FAIL_RATE}", flush=True)

    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    srv.daemon_threads = True
    threading.Thread(target=_heartbeat, args=(args.port,), daemon=True).start()
    print(f"[mock] fake ComfyUI listening on http://127.0.0.1:{args.port}  (Ctrl-C to stop)",
          flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("[mock] shutting down", flush=True)
        srv.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
