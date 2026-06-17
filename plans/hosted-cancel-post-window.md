# Tighten hosted-take cancel during the create-POST window

Card: 6a31f054 (follow-up from 6a31a03f / PR #26)

## Context

`JobManager.request_stop` (backends/jobs.py) stops an in-flight GENERATING render. For a
hosted (Replicate) take it issues `replicate_client.cancel_prediction(t.backend_job_id)`.
But `backend_job_id` (the prediction id) isn't recorded until `on_submit` fires — which is
only *after* the create-POST returns (a second or two). If a stop is requested during that
window, `t.backend_job_id` is still empty, so request_stop's cancel branch is **skipped**.

`request_stop` still flags the take in `_stopping` and returns True (UI reports "stopped"),
but `_stopping` only maps a *subsequent backend error* to CANCELLED. A prediction that
**succeeds** in that window lands DONE on the success path (which never checks `_stopping`),
keeps spending, and its `.mp4` is then orphaned when the shot delete that triggered the stop
removes the shot.

## Design

Close the window from the worker side, as the card prescribes: re-check `_stopping` right
after `on_submit` records the prediction id and self-cancel if flagged.

- **backends/jobs.py** — add `JobManager.is_stop_requested(take_id) -> bool` (reads the
  shared `_stopping` set; GIL-atomic, no lock needed, like the other `_stopping` reads).
- **ui/main_window.py** (`_make_runner`, replicate branch) — move `on_submit` *inside*
  `runner` so it captures `progress`. After recording `backend_job_id`, if
  `self.jobs.is_stop_requested(take_id)`: log a milestone line and best-effort
  `replicate_client.cancel_prediction(pid)` (swallow errors, mirroring request_stop). The
  subsequent poll loop in `run_prediction` then sees status `canceled` → raises
  `ReplicateError` → `GenerationJob.run`'s except sees `tid in _stopping` → records
  CANCELLED. Spend halts.

This composes with the existing request_stop path; together they cover every interleaving:
- stop before on_submit: request_stop skips (no id yet), on_submit self-cancels. ✓
- stop after on_submit: request_stop cancels via the recorded id (existing path). ✓
- interleaved: both may fire; `cancel_prediction` is server-side idempotent. ✓

ComfyUI is unaffected — its `request_stop` calls `stop_work()` (interrupts the current
prompt; needs no backend_job_id), so there's no analogous gap.

## Tests

Extend `scripts/smoke_phase2.py`:
- `test_is_stop_requested` — request_stop on a GENERATING hosted take with **no**
  backend_job_id flags `_stopping`, sends no cancel, and `is_stop_requested` returns True.
- `test_stop_during_submit_window` — drive a real `GenerationJob` through a fake replicate
  runner whose `on_submit` mimics production (record id → check `is_stop_requested` →
  fake-cancel). Pre-flag the take in `_stopping`; assert the fake cancel fired and the take
  lands CANCELLED, not DONE.

## Out of scope

- The separate "worker already past the backend call → lands DONE" media-binning gap (a
  different narrow window the card's note also mentions).
- ComfyUI (no gap).
- Any change to request_stop's existing recorded-id path.

## Verification

Headless smoke suite (all six phases). A live hosted take is NOT fired (spends money) —
the race is exercised offline via the fake-backend GenerationJob test.
