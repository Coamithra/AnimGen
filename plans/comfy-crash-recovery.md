# Plan: ComfyUI crash detection + auto-recovery

Card: **comfy crash detection** (6a31ed57) — https://trello.com/c/OiOdcgC2

## Context

Over a long series of local renders, ComfyUI sometimes dies mid-render — the 12GB card's
2s GPU watchdog (TDR) can still occasionally fire on a 14B render even with our preventive
`--disable-dynamic-vram` flag, taking the whole server process down with it. Today this just
fails the take, and because the local pool is serialized, every *remaining* queued take then
fails one-by-one on `preflight()` (server unreachable). A whole overnight batch is lost.

The card wants the app to **detect** the crash, **restart** ComfyUI, **requeue** the failed
take and the rest, surface a **"failed in XmYs, retrying"** note in the queue, and — to avoid
an infinite restart loop when ComfyUI is *legitimately* broken — **abandon/pause the whole
local queue after a single take crashes 3 times.**

### Root cause / current behaviour (traced)

- A local render is polled inside an AnimGen worker thread (`comfy_client.submit` →
  `_poll_until_done`, every 5s `GET /history/{pid}`). When the server process dies, that GET
  raises `URLError` → `ComfyError("ComfyUI unreachable…")`, which propagates up through
  `generate` → the runner → `GenerationJob.run`'s `except` → the take is marked `failed`.
- The local `QThreadPool` has `maxThreadCount=1`. The remaining `pending` takes are still
  queued in the pool; when the failed worker returns, the next one starts — but the server is
  dead, so its `preflight()` raises and it fails too. Cascade to total failure, no restart.

### Key insight for the fix

Because the local pool is **already serialized**, retrying a crashed take *inside the same
worker* (block, restart the server, re-run) makes "requeue the rest" automatic — the other
queued takes simply wait behind the blocked worker; they are never dequeued or lost. No
explicit re-enqueue is needed.

## Design

### New module — `backends/crash_recovery.py` (pure, dependency-injected → headless-testable)

```
MAX_ATTEMPTS = 3
class QueueAbandoned(RuntimeError): ...
def format_elapsed(secs) -> str            # 75 -> "1m15s", 45 -> "45s" (matches queue_view style)
def run_with_crash_recovery(*, render, server_running, restart_server,
                            note, on_abandon, clock, max_attempts=MAX_ATTEMPTS) -> dict
```

Loop, per take (one call wraps one take's whole render incl. retries):
1. `render()` → return its result on success.
2. On exception, check `server_running()`:
   - **True** → the server is alive, so this is a *genuine workflow error* (bad node, etc.),
     **not** a crash → re-raise unchanged. Only this take fails; the next take proceeds.
   - **False** → the server is gone → a **crash**.
3. Crash handling:
   - If this was the last allowed attempt (`attempt >= max_attempts`): `note(reason)`,
     `on_abandon(reason)`, raise `QueueAbandoned` (the running take then fails via the normal
     path; the rest were just cancelled by `on_abandon`).
   - Else: `note("comfy crashed — failed in {elapsed}, retrying (attempt n+1/3)")`, then
     `restart_server()`. If `restart_server()` itself raises (e.g. ComfyUI didn't come back),
     `on_abandon` + `QueueAbandoned` (can't recover without a server).

Detection signal = **"server is not running at the moment of failure"** (`server_running()`),
the reliable signature of a TDR crash (process dies). A failure with the server still up is
deliberately treated as a normal per-take error, so a genuinely broken workflow doesn't trip
an endless restart loop. (3-strike abandon is the backstop for a server that crashes every time.)

### `backends/comfy_client.py` — server lifecycle helpers (additive)

```
def wait_until_responsive(timeout_s=120, poll_s=2.0) -> bool   # poll server_status() until running
def restart_server(progress_cb=None, ready_timeout_s=120) -> None
```

`restart_server`: best-effort `stop_server()` (kills our proc or whatever's on the port —
swallow `ComfyError` if already down) → `launch_server()` (always re-applies the safe
`--disable-dynamic-vram --cache-none` flags via `build_launch_command`) → block on
`wait_until_responsive`; raise `ComfyError` if it never answers. This means the restart also
*fixes the crash's root cause* (relaunched with dynamic-VRAM off), and each retry's
`generate()` re-runs `preflight()`, so the dynamic-VRAM gate is never weakened.

### `backends/jobs.py` — pause the local queue

- New signal `queue_abandoned = Signal(str)` (on `_JobSignals` + re-exposed on `JobManager`).
- New `abandon_local(reason) -> int`: `self._local_pool.clear()`, then mark every still-`pending`
  **comfyui** take `cancelled` with `reason` (and add to the `_cancelled` safety-net set);
  emit `queue_abandoned`. **Hosted takes are untouched** — a dead GPU doesn't affect Replicate.
  Safe to call from the worker thread (`clear()` only drops *queued* runnables, not the active
  one; `update_take` is RLock-guarded; the signal auto-queues to the GUI thread).

### `ui/main_window.py`

- Wrap the **comfyui** branch of `_make_runner` with `crash_recovery.run_with_crash_recovery`,
  injecting: `render` = the existing frame+`comfy_client.generate` closure; `server_running` =
  `comfy_client.server_status()["running"]`; `restart_server` =
  `comfy_client.restart_server(progress_cb=progress)`; `note` = `progress`; `on_abandon` =
  `self.jobs.abandon_local`; `clock` = `time.time`. (Hosted branch unchanged.)
- Connect `self.jobs.queue_abandoned` → `_on_queue_abandoned`: log it, refresh the Cancel
  action, and pop a non-modal `QMessageBox.warning` ("ComfyUI queue paused").

The `note(...)` lines flow through the existing `progress(line)` path, so they appear both in
the **Queue tab**'s Progress column for the retrying take *and* the main log ("maybe also the
general logs"). The take stays `generating` across retries (it never momentarily flips to
`failed`), so the queue shows a continuous "…retrying (attempt 2/3)" rather than a flicker.

### Tests — `scripts/smoke_phase2.py` (`test_crash_recovery`, registered in `main()`)

All headless, no real ComfyUI / GPU / sleeps (fakes + injected `clock`):
- `format_elapsed` — seconds → "Ys"/"XmYs"/"XhYmZs".
- `run_with_crash_recovery`: (a) success first try → no restart; (b) crash once then succeed →
  1 restart, note says "attempt 2/3"; (c) crash 3× → `QueueAbandoned`, `on_abandon` called
  once, 2 restarts; (d) failure with server **up** → original exception propagates, no restart/
  abandon; (e) `restart_server` raises → `QueueAbandoned` + `on_abandon`.
- `comfy_client.wait_until_responsive` with a stubbed `server_status` that flips to running.
- `comfy_client.restart_server` orchestration with `stop_server`/`launch_server`/
  `wait_until_responsive` stubbed (verifies call order + the "didn't come up" → `ComfyError`).
- `JobManager.abandon_local` integration (mirrors `test_cancel_pending`): a running local
  blocker + 2 pending local + 1 pending **hosted**; `abandon_local` cancels the 2 local
  pending, leaves the hosted pending alone, fires `queue_abandoned`.

### CLAUDE.md

Add a new "Hard-won rules" entry documenting local crash recovery (detection = server-down,
restart via `build_launch_command`, 3-strike `abandon_local`, hosted untouched) and update the
`backends/` rows in the architecture map (`crash_recovery.py`; `jobs.abandon_local`/
`queue_abandoned`; `comfy_client.restart_server`/`wait_until_responsive`).

## Out of scope

- Hosted (Replicate) retry/restart — different failure model; untouched.
- Distinguishing TDR specifically from any other server-down cause (we treat *any* server-down
  failure as a recoverable crash; that's the right action regardless of why it died).
- Configurable attempt count / backoff UI — `MAX_ATTEMPTS = 3` per the card, constant.
- Auto-resuming the queue after an abandon — abandon is terminal-for-this-batch by design
  ("so we don't get into a restart loop"); the user re-Generates once they've checked the GPU.

## Verification

- `scripts/smoke_phase1-6.py` all green (gate), with the new `test_crash_recovery` coverage.
- Manual (offline): exercised via the unit fakes. A *live* crash-and-restart needs a real GPU
  render — flagged for the user as the one path not covered headlessly.
