# Cancel/clean up in-flight take when its shot is deleted

Card: 6a31a03f (https://trello.com/c/hf9ZmnLs)

## Context
`MainWindow.delete_shot` removes a shot and its takes from the index. If a take for
that shot is still **PENDING** (queued) or **GENERATING** (mid-render), the JobManager
worker keeps the `take_id`: the eventual `.mp4` lands in `.assets/takes/` and
`project.update_take` no-ops (take already gone), so the file is orphaned. There is no
per-shot cancel today — `cancel_pending` cancels ALL queued takes across every shot.

Decision (user): on shot delete, **warn, then proceed, AND actually stop** any in-flight
render — don't just abandon it to orphan a file and keep spending GPU/$.

## Design

### `backends/jobs.py`
- `JobManager._stopping: set[str]` — take ids whose in-flight render we asked to stop.
- `GenerationJob` takes the `stopping` set; in its `except` block, if `tid` is in
  `cancelled` OR `stopping`, mark the take **CANCELLED** (not FAILED) — a backend error
  raised because we cancelled it is an intentional stop, not a failure. `finally`:
  `stopping.discard(tid)`.
- `JobManager.cancel_shot_takes(shot_id) -> int` — cancel every still-PENDING take of a
  shot (same mechanism as `cancel_take`: add to `_cancelled`, mark CANCELLED, emit). For
  the queued takes that haven't started — prevents the orphaned `.mp4`.
- `JobManager.request_stop(take_id) -> bool` — for a GENERATING take, add to `_stopping`
  and issue the backend-side stop (best-effort, swallow errors; backend read from the
  take's `settings_snapshot["backend"]`):
  - `comfyui` → `comfy_client.stop_work()` (interrupt the running prompt; the local pool
    serializes so only this take's prompt is in ComfyUI's queue).
  - `replicate` → `replicate_client.cancel_prediction(take.backend_job_id)` if the
    prediction id was recorded. Stops spend server-side; the worker's next poll sees
    `canceled`, raises, and the `except` maps it to CANCELLED.

### `backends/replicate_client.py`
- `cancel_prediction(pred_id, token=None)` — POST `/predictions/{id}/cancel` (best-effort).
- `run_prediction(..., on_submit=None)` — call `on_submit(pred_id)` right after the create
  POST (mirrors comfy's `on_submit`) so the take records `backend_job_id` mid-render and
  becomes cancellable. `generate(..., on_submit=None)` threads it through.

### `ui/main_window.py`
- `_make_runner` (replicate branch): pass
  `on_submit=lambda pid: self.project.update_take(take_id, backend_job_id=pid)` to
  `replicate_client.generate(...)`.
- `delete_shot`: before deleting, find GENERATING takes for the shot. If any, add a
  warning line to the confirm dialog ("N take(s) are mid-render and will be stopped.").
  After confirm: `cancel_shot_takes(shot_id)` (queued), then `request_stop(t.id)` for each
  in-flight take, then `project.delete_shot(shot_id)`, then refresh + `_refresh_cancel_action`
  + `queue_tab.refresh()`. Log how many were cancelled/stopped. (request_stop and
  cancel_shot_takes run BEFORE delete_shot — they read take backend/job-id from the index.)

## Tests (`scripts/smoke_phase2.py`)
- `cancel_shot_takes`: seed PENDING takes across two shots; cancel shot A's; assert only A's
  flip to CANCELLED and shot B is untouched; return count correct.
- in-flight stop mapping: build a `GenerationJob` whose runner raises, pre-add its id to a
  JobManager's `_stopping`, run it directly; assert the take ends CANCELLED, not FAILED.
- `request_stop` best-effort: monkeypatch `comfy_client.stop_work` / `replicate_client.
  cancel_prediction` to record calls; assert request_stop adds to `_stopping`, calls the
  right backend, and swallows backend errors (no raise) — hermetic, no network/GPU.

## Out of scope
- Binning take **media** on shot delete (separate "delete_shot does not bin take media" gap).
- Force-killing a worker thread (cancellation stays cooperative via the backend stop +
  the worker's next poll).
