# Overnight batch render

Trello: **Overnight batch render** (`6a326852`)

## Context

Today generation is strictly one-shot-at-a-time: `MainWindow.generate_shot(shot_id)`
confirms one item via `confirm_launch([item])` and enqueues one take. To run an
unattended overnight batch you'd have to click Generate (and the cost gate) once per
shot. The infrastructure for unattended rendering already exists — the local queue is
serialized, crash-recovery auto-restarts ComfyUI and 3-strike-abandons, takes persist
write-through — so the only gaps are **bulk enqueue**, **N takes per shot**, **auto
power-down on drain**, and a **morning report**.

Decided with the user: full scope, **both backends**, power action max = **sleep PC**.

## Design

### New pure module — `backends/batch.py` (no Qt; dependency-injected, headless-testable)
- Power-action constants: `POWER_NONE`, `POWER_STOP_COMFY`, `POWER_SLEEP`.
- `plan_batch(shots, *, takes_per_shot, model_of, aspects_of, est_of) -> BatchPlan`
  where the injected callables resolve model / valid aspects / cost. Returns
  `items` (the `confirm_launch` item dicts, **N per eligible shot**),
  `eligible` (list of `(shot, model, settings, est)`), and
  `skipped` (list of `(shot_name, reason)` — unknown model / invalid aspect / no start frame).
  Mirrors the eligibility checks in `generate_shot`.
- `build_batch_report(rows, *, started, finished, power_action) -> str` —
  rows = `[{name, status, cost_actual}]`; returns a plain-text summary (counts by
  status, total actual cost, elapsed). Pure → unit-tested.
- `sleep_command() -> list[str]` — the OS suspend argv (Windows:
  `rundll32.exe powrprof.dll,SetSuspendState 0,1,0`).

### `BatchRun` (lightweight controller, in `ui/main_window.py`)
Tracks one in-flight batch: `take_ids: set`, `remaining: set`, `power_action`,
`started: iso`. `mark(take_id, status)` discards terminal takes from `remaining`;
batch is complete when `remaining` is empty. Terminal = DONE / FAILED / CANCELLED
(covers normal finish, workflow error, cancel, and 3-strike abandon — which cancels
its pending takes, emitting `status_changed(CANCELLED)`).

### `MainWindow` wiring
- **Refactor**: extract `_queue_take(shot, model, settings, est) -> take_id` out of
  `generate_shot` (snapshot build + `add_take` + `enqueue` + log). Resolve a fresh
  random seed *inside* `_queue_take` per call, so N takes of a random-seed shot vary.
  `generate_shot` calls it after its single `confirm_launch`.
- **New control-strip action** "Generate batch…" → opens `BatchDialog`.
- `start_batch()`: collect shots (all in project, or current filtered view),
  `batch.plan_batch(...)`; if nothing eligible, warn and stop; if some skipped, show
  them. Fire **one** `confirm_launch(items)` (honors the cost gate with a single
  up-front confirm). Save the project once (untitled → Save As). Loop `_queue_take`
  for every (eligible shot × N). Stash `self._batch = BatchRun(...)`. Refresh.
- **Drain detection**: in `_on_status_changed`, if `self._batch` and the take is in it
  and status is terminal, `self._batch.mark(...)`; when complete, `_finalize_batch()`.
- `_finalize_batch()`: build rows from `project.get_take`, write
  `paths.EXPORTS_DIR / overnight_<ts>.txt`, log the path, clear `self._batch`, then
  `_perform_power_action(action)` (after a short `QTimer.singleShot` grace so writes
  flush). Power action runs on a daemon thread (stop_server can take ~10s): always
  `comfy_client.stop_server()` for both stop/sleep; for sleep, then run
  `batch.sleep_command()` via subprocess. All best-effort (try/except + log).

### `BatchDialog` (`ui/batch_dialog.py`)
Thin Qt dialog: scope radio (All shots / Current view), takes-per-shot spin (1–20),
"When finished" combo (Do nothing / Stop ComfyUI / Sleep PC). Returns
`(scope, takes_per_shot, power_action)`. No `.exec()` in tests — logic lives in `batch.py`.

## Tests

Extend `scripts/smoke_phase2.py` (already imports `cost_confirm` + jobs):
- `plan_batch`: eligibility filtering (unknown model / bad aspect / missing start frame
  skipped), N-per-shot item count, item shape matches `confirm_launch`.
- `build_batch_report`: counts + totals + elapsed for a mixed done/failed/cancelled set.
- `sleep_command` returns a non-empty argv.
- `BatchRun.mark` completion semantics (terminal-only; complete when remaining empty).

## Out of scope
- Per-shot scheduling / start-time delays.
- Hibernate / shutdown power actions (sleep is the max).
- Resuming a batch across an app restart (orphan recovery already reconciles takes;
  the BatchRun controller itself is in-memory and not persisted).
- Changing hosted concurrency or local serialization.

## Verification
- Headless: all six smoke phases; new batch coverage in phase 2.
- Manual (UI): open the Generate batch dialog, confirm the single cost gate shows N×shots
  and the right total, queue a batch, watch it drain in the Queue tab, confirm the report
  file appears in `data/exports/`. Power action / sleep verified by the user on real HW.
- No live take fired without explicit go-ahead.
