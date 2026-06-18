# Plan: Restart cancelled takes (Trello #49)

## Context
Cancelled takes — most importantly the batch of PENDING takes orphan-recovery cancels after a
mid-batch crash/restart — must currently be re-fired by hand, shot by shot. Add a one-click
"Restart cancelled takes" action (project-wide button + per-take context entry) that re-runs
them through the same cost-confirm gate as Generate.

Each take keeps its immutable `settings_snapshot` (model/backend/replicate id/workflow,
start+end frames, prompt+negative, resolved settings incl. the concrete seed, and — for takes
made on/after 2026-06-17 — the framing `canvas [w,h]` + `crop`). So a restart can rebuild the
runner straight from the take's own snapshot, no shot reconstruction needed.

## Final scope (user decision 2026-06-18): exact restart in place, else mark FAILED
"Keep it simple — exact restart in place; if assets are no longer available then just set it to
failed with a message." So there is **no fresh-fallback / new-take path**:
- **Exact-restartable take → re-run it IN PLACE** (flip CANCELLED→PENDING, re-enqueue a runner
  built straight from the immutable snapshot — same seed, same framing). No take-row multiplication.
- **Not exact-restartable → mark the take FAILED** with an explanatory `error` message (so the user
  knows why), instead of silently skipping or re-generating from the shot.

## Eligibility
Per cancelled take:
- **restartable** — snapshot is new-format (has both the `canvas` and `crop` keys, added 2026-06-17)
  AND its `model_id` is still in the roster AND its `start_frame` file exists.
- **unrestartable (reason)** — otherwise: snapshot predates framing-in-snapshot / unknown model /
  start keyframe no longer available. These get set to FAILED with the reason as the take error.

## File-by-file

### `backends/restart.py` (new, pure / headless-testable)
- `@dataclass RestartPlan(items, restartable, unrestartable)`:
  - `items: list[dict]` — `confirm_launch` item dicts (name/model_display/backend/est_cost/params),
    one per restartable take.
  - `restartable: list` — the takes that can be replayed exactly.
  - `unrestartable: list[tuple]` — `(take, reason)`.
- `plan_restart(takes, *, model_of_id, est_of, path_exists, name_of)` — dependency-injected
  callables (no library/project import at call time), so it's unit-testable.
  `_has_framing(snap)` = `"canvas" in snap and "crop" in snap` (the keys added 2026-06-17; a
  new-format take is replayable even with a `[None,None]` canvas — `render_keyposes` defaults to
  1254 both times). Reason order: no-framing → unknown-model → start-frame-missing.

### `backends/jobs.py`
- `restart_take(take_id, backend, runner)` — discard the id from `_cancelled`/`_stopping`/
  `_requeue` (a cancelled take's id is in `_cancelled`, which would make the worker bail), then
  `enqueue(...)`. Mirrors `resume_local`'s `_cancelled.discard`.

### `ui/main_window.py`
- Control-strip `QAction "Restart cancelled takes"` (after Cancel pending), enabled when the
  project has ≥1 cancelled take. Add `_refresh_restart_action()`; call it everywhere
  `_refresh_cancel_action()` is called.
- `restart_cancelled_takes()` — gather all CANCELLED takes (project-wide, `include_deleted=False`,
  ignoring view filters, like Cancel-pending) → `self._restart_takes(takes)`.
- `_restart_takes_by_ids(ids)` — context-menu entry: filter the ids to cancelled takes →
  `self._restart_takes(...)`. Wired to the `restart_requested` signal from cards/tabs.
- `_restart_takes(takes)` — `restart.plan_restart(...)`; if `plan.restartable`, run the rule-#1
  `confirm_launch(plan.items)` gate (abort everything on Cancel — fail nothing) then `save_project()`
  and `_restart_in_place` each; then mark every `unrestartable` take FAILED with its reason. Refresh.
  Show an info box only when something was marked failed (the happy path stays quiet).
- `_restart_in_place(take)` — model = `library.get_model(snap["model_id"])`; settings =
  `snap["settings"]`; build a synthetic `Shot` from the snapshot (`_shot_from_snapshot`); reset the
  take (status=PENDING, error/started/completed/video_path/thumbnail/preview_gif/cost_actual=None);
  `jobs.restart_take(take.id, model["backend"], self._make_runner(model, synth, settings, take.id))`.
- `_shot_from_snapshot(shot_id, snap)` — returns a `store.models.Shot` carrying the snapshot's
  frozen start/end frame, canvas, crop, prompt, negative, model_id, settings. `_make_runner` and
  `framing.render_keyposes` both read only these fields, so the snapshot replays exactly.
- `_take_label(take)` — shot name if the shot still exists else `take.id[:8]` (for items + reasons).

### `ui/takes_view.py`
- Add `restart_requested = Signal(list)`.
- Refactor `_context_menu(pos)` to build the menu via a new `_build_context_menu(ids)` helper
  (returns a wired `QMenu`, no `exec()`), so it's headless-testable (rule #4 / shot_card pattern).
  `_context_menu` just calls `_build_context_menu(ids).exec(...)`. Add a "Restart take(s)" action
  only when any selected take is CANCELLED, emitting `restart_requested.emit([cancelled ids])`.

### `ui/shot_card.py` + `ui/shot_tab.py`
- Forward `restart_requested` from the embedded `TakesView` (mirror `open_take_requested`).

### `ui/main_window.py` wiring
- `card.restart_requested.connect(self._restart_takes_by_ids)` and
  `tab.restart_requested.connect(self._restart_takes_by_ids)`.

## Must respect (non-negotiable)
- **Cost gate (rule #1):** restart routes through `confirm_launch(plan.items)`; default Cancel; no bypass.
- **Local preflight (rule #10):** snapshot restart reuses `_make_runner`'s comfy path, so
  `ensure_server` + `preflight()` + crash recovery still apply unchanged.
- **`settings_snapshot` immutable (rule #3):** in-place restart only flips status/timestamps/output
  fields — never the snapshot. Fresh fallback makes a brand-new take + snapshot.
- **RLock'd atomic writes (rule #9):** all status flips go through `project.update_take`.

## Tests (headless, `scripts/smoke_phase2.py`)
- `test_restart_plan` — pure `plan_restart`: restartable (new-format snapshot, model known, frame
  exists); unrestartable reasons (no framing / unknown model / start frame missing); items shape.
- `test_restart_take` — `JobManager.restart_take` discards a cancelled id and drives a fake runner
  to DONE (proves the worker doesn't bail on the stale `_cancelled` membership).
- `test_restart_from_snapshot` — MainWindow-driven (stub `confirm_launch`/`save_project`/info box,
  fake `_make_runner`): a cancelled take with a full snapshot restarts IN PLACE (same id; runner
  built from the snapshot's seed/canvas, not a re-roll); an unrestartable cancelled take is marked
  FAILED with a message. Plus `_build_context_menu` builds a Restart action for a cancelled take
  without `exec()`.

## Out of scope
- Fresh re-Generate / re-roll / re-frame fallback (user chose mark-failed instead).
- Auto-resume on launch (recovery deliberately cancels never-submitted takes; this stays a manual,
  gated re-run).
- Persisting the in-memory `BatchRun` across restarts.

## Verification
- All 7 headless smoke phases pass.
- Manual (no spend): launch app, confirm the button enables only with ≥1 cancelled take, the
  cost gate appears, and a cancelled take flips back to pending. A live take is NOT required.
