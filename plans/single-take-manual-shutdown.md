# Card #42: Single (non-batch) local take auto-restarted on manual ComfyUI shutdown

## Context
Card #41 made a deliberate ComfyUI *Stop working* / *Shut down* pause an active **overnight
batch**, so crash-recovery (rule #12) no longer auto-restarts the server out from under the
user. But the same conflict remains for a **non-batch** local queue: with one or more single
local takes queued (not via *Generate batch…*), a manual *Shut down* takes the server down →
crash-recovery reads the render failure as a crash → relaunches the server and retries (up to
3 strikes). So a deliberate shutdown is still fought when no batch is active.

Root cause: `MainWindow._pause_batch_if_running` (wired to `comfy_tab.stop_intent`) only sets
the pause flag when `self._batch is not None`.

## Decision
Implement **option (a)** from the card: on a deliberate ComfyUI stop with non-batch local work
in flight, pause the local queue (so crash-recovery treats the failure as the intended stop),
and **auto-clear** the pause once the in-flight take drains — no resume UI for the single-take
case. The queued (not-yet-started) local takes are **cancelled** (there is no resume
affordance, and leaving them PENDING would either zombie the queue or re-launch the server the
user just stopped via `ensure_server`).

Why not hold-PENDING like the batch case: without a Resume button the held takes would never
re-run, so the auto-clear condition ("local queue drained") could never become true → the
pause flag would stick True forever, breaking real crash-recovery on the next render. Cancel +
auto-clear keeps the flag transient.

## Design (file-by-file)

### `backends/jobs.py`
- Add `clear_local_pause()` — sets `_local_paused = False` without re-enqueuing anything (the
  non-batch drain path; distinct from `resume_local`, which re-enqueues held takes).

### `ui/main_window.py`
- New `__init__` field `self._stop_paused_local = False` — marks a transient non-batch pause so
  `_on_status_changed` knows to auto-clear it.
- Rename `_pause_batch_if_running` → `_pause_local_on_stop_intent` (the handler now covers the
  non-batch case too); update the `stop_intent.connect` wiring + docstring.
  - Batch branch: unchanged (pause + hold for Resume batch).
  - Non-batch branch: if there is local (comfyui) work in flight (GENERATING or PENDING) and we
    aren't already in a stop-pause, call `jobs.pause_local()` (sets flag, clears the pool,
    returns the queued PENDING ids), set `self._stop_paused_local = True`, and `cancel_take`
    each held id. The GENERATING take is left to fail cleanly (no restart).
- New helper `_local_work_in_flight()` — any comfyui take still GENERATING/PENDING.
- `_on_status_changed`: when not in a batch and `self._stop_paused_local` and the local queue
  has drained (`not _local_work_in_flight()`), clear the marker and call `jobs.clear_local_pause()`.

Note: a worked take is GENERATING (the PENDING→GENERATING transition is the first line of
`GenerationJob.run`, before the runner), so `pause_local`/`cancel_take` (PENDING-only) never
touch a take a worker is actively running — cancelling the held PENDING ids is safe.

## Tests (`scripts/smoke_phase2.py`)
- `clear_local_pause` clears the flag (pure, on `JobManager`).
- A non-batch scenario mirroring `test_pause_resume_local`: a GENERATING local take + queued
  PENDING local takes; simulate the stop-intent path (`pause_local()` + cancel the held +
  set/observe the drain → `clear_local_pause`), asserting: queued takes end CANCELLED, the
  in-flight take's failure does not restart, and `is_local_paused()` returns to False once
  drained.

## Out of scope
- The overnight-batch case (already handled by card #41).
- Any new Resume/pause UI for single takes (option b) — explicitly not doing the general
  "pause local queue" affordance.
- Changing *Stop working* semantics (server stays up → already a clean workflow-error path,
  no restart); the pause is harmless there and uniformly covers both buttons.

## Verification
- Headless smoke suite (all 7) green.
- Manual (optional, no spend): queue a single local take, Shut down ComfyUI mid-render, confirm
  the server is not relaunched and the take ends FAILED (not retried). Requires a live ComfyUI.
