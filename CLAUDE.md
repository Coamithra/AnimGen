# AnimGen — pickup guide

Native **PySide6 desktop app** that turns keyposes into game-ready 2D animations.
Work is organized into **projects** (`.animproj` files); each project holds **shots**
(start/end keyframe + framing + prompt + model + settings) and a library of keyframe
**assets**. Fire **generations** on hosted (Replicate) or local (ComfyUI) backends behind
a **cost-confirm gate**, **triage** the resulting **takes** in a folder view
(star / delete-to-bin / filter), and **export** selected takes as frame sets + a
settings record. Extracted from the *Fighter* sprite project on 2026-06-13.

**Nomenclature (renamed 2026-06-16):** *Project* → *Shot* → *Take*. A **Shot** is the
authored spec (was "config"); a **Take** is one generated `.mp4` (was "result").
**Assets** are the project's keyframe images, kept flat in the `.assets/` sidecar; a shot
references assets for its start/end keyframes.

## Project links

- **GitHub:** https://github.com/Coamithra/AnimGen  (public, `main`)
- **Trello (build board):** https://trello.com/b/7SycR6UZ  ("Animation Generator Tool").
  **When grabbing a ticket from Trello, follow @CONTRIBUTING.md** — it's the
  card-pickup runbook (two-phase claim handshake, worktree flow, smoke-gate, ship steps).
- Origin/source project: *Fighter* (`../Fighter`), referenced at runtime — see "External wiring".

## Status (2026-06-16)

Built in 6 phases, **all 7 headless smoke suites pass** (`scripts/smoke_phase1-7.py`;
phase 7 covers the `remote/` control server).
Storage is **file-based `.animproj` documents** (Project → Shots → Takes), replacing the
old single SQLite DB. Shots reference **assets** (keyframe images imported into the
project's `.assets/`); the 1254² contract keypose is framed **at generation time** from
the asset + the shot's crop params (no baked files). The seeder writes a starter
`data/Fighter.animproj` with 31 shipped moves, importing their keyframes as assets.
**Still pending (needs explicit go-ahead — it spends money / GPU):** a live hosted take
and a live local take. The backends are verified offline only.

## Run / test / seed

```bash
# from the repo root
.venv/Scripts/python.exe app.py                      # launch the app (Windows)

# headless smoke tests (no spend, no GPU, no real renderer)
for n in 1 2 3 4 5 6 7; do QT_QPA_PLATFORM=offscreen PYTHONIOENCODING=utf-8 \
  .venv/Scripts/python.exe scripts/smoke_phase$n.py; done

# (re)build the starter project from the shipped-move manifest (idempotent;
# delete data/Fighter.animproj + data/Fighter.assets first for a clean rebuild)
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe scripts/seed_configs.py

# live-drive the running GUI without full-PC control (see "Driving the app" below)
ANIMGEN_REMOTE=1 .venv/Scripts/python.exe app.py        # launch with the control server
python scripts/remote_cli.py snapshot                  # list drivable widgets
python scripts/remote_cli.py shot out.png              # screenshot the window
python scripts/remote_cli.py set --ref mainTabs --value Assets   # switch tab
```

Setup if `.venv` is missing: `python -m venv .venv` then
`.venv/Scripts/python.exe -m pip install -r requirements.txt`.

### Driving the app (agents: use this to live-test, don't ask for PC control)

**If you (an agent) need to interactively test the GUI — click things, switch tabs,
read on-screen state, take screenshots — use the built-in control server, NOT desktop
computer-use / full-PC-control.** It's the supported flow and scoped to AnimGen:

1. Launch with the server on: `ANIMGEN_REMOTE=1 .venv/Scripts/python.exe app.py`
   (background it; it logs `Remote control listening on http://127.0.0.1:<port>`,
   default 8765 via `ANIMGEN_REMOTE_PORT`).
2. Drive it with `scripts/remote_cli.py` (or `curl`): `snapshot` (DOM-like widget list),
   `shot <file>` (PNG screenshot via `QWidget.grab()`), `click`/`type`/`key`/`set`
   targeting a widget by `--ref` / `--object-name` / `--text`.
3. Read the PNG back to *see* the result; re-`snapshot` to confirm state.
4. Stop it by `taskkill //F //PID <pid>` for **that** instance's port (find it with
   `netstat -ano | grep :<port>` — never blanket-kill `python.exe`, other agents share it).

Still the rules: **a live hosted/local *take* spends money / GPU — explicit go-ahead only.**
Driving the UI (tabs, framing, dialogs) is free; only the Generate launch costs. The
cost-confirm gate still appears and must be driven, never bypassed. Headless `smoke_phase*`
remains the gate for logic; the control server is for *interactive* UI verification.
Full mechanism + invariants in **Hard-won rule #13**.

### Offline GPU-free harness (mock ComfyUI)

`scripts/mock_comfy.py` is the supported way to drive **real local batches with NO GPU**. It
stands up a fake ComfyUI that speaks just enough of the HTTP+WS protocol — `/system_stats`
with safe argv so `preflight()` passes, `/prompt`, `/history`, `/queue`, `/interrupt`, and a
hand-rolled `/ws` streaming `progress` + binary preview frames — for the **unmodified** app to
queue, render, and claim takes against it. Each fake render finishes in ~8s and serves a real
canned `.mp4` the app copies + extracts frames from (genuine CPU load). Use it for long offline
soak tests (does the app survive without the GPU? widget/memory growth — pair with the `applog`
heartbeat `max_widgets=` census, rule #18) and for fast iteration on the local-render / Queue-tab
UI without spending GPU time.

```bash
# keep the REAL ComfyUI down so 8188 is free; honors ANIMGEN_COMFY_DIR
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe scripts/mock_comfy.py --delay 8 --jitter 3
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe scripts/mock_comfy.py --fail-rate 0.25
```

then launch the app and fire **Generate batch…**. **`--fail-rate F`** (0..1) makes that fraction
of renders return a `status_str=="error"` `/history` entry (an `execution_error`, no outputs) so
the app records a **FAILED** take — the server stays **up**, so it reads as a genuine workflow
error (`interrupted=False`, not a crash), exercising crash-recovery's "up == not a crash"
discrimination (rule #12) and the FAILED-take / queue-continuation path offline. Because these are
*genuine* failures (not crash-*interrupted* takes), they do **not** feed the **Restart interrupted
takes** action (rule #17). It also does **not** simulate a server-death crash (that needs the real
ComfyUI lifecycle, since `restart_server` relaunches via `build_launch_command`). GPU-free, no
spend — but the cost-confirm gate (rule #1) still appears and must be driven.

## Architecture map

| Path | Role |
|---|---|
| `app.py` | entry point (adds repo root to `sys.path`; opens the last/seeded/new Project, shows MainWindow). Wires up `applog` diagnostics + `_force_software_rendering()` (Windows software-GL so a GPU TDR can't kill the UI — rule #15) before `QApplication` |
| `paths.py` | all paths + external-location config (see below); `DEFAULT_PROJECT`, `SCRATCH_DIR`, `APP_STATE` |
| `library.py` / `model_library.json` | model-roster loader (+ `aspect_ratios(model_id)`, `sync_model_capabilities` — writes the two refresh-derived capability flags back into the roster, atomic+lock-guarded) + the roster (authored IDs/costs/notes/`aspect_ratios`/`supports_end_frame`, plus auto-synced `supports_negative_prompt`/`supports_camera_fixed`; local `comfy_nodes.size_node`) |
| `store/project.py` `store/models.py` | file-based **Project** document (shots / takes / jobs) + dataclasses (`Shot`/`Take`/`Job`). Hybrid persistence: shot edits buffer (`dirty`, saved on `save()`); takes write through to `<assets>/takes.json`. Keyframe **assets** (`list_assets`/`import_asset`/`remove_asset` — image files flat in `.assets/`) + a load-time migration that flattens old `keyposes/<hash>/` baked files (`_migrate_flatten_keyposes`: re-points the shot to an imported copy of its source, **persists the re-point write-through (`_write_project_file`) BEFORE deleting the keyposes sources** so a Discard at the next save-prompt can't strand a half-applied, source-deleted migration — and so the freshly-loaded project stays clean, no phantom `*`; a failed persist keeps the sources and falls back to `dirty=True`). Carries an optional `ui_state` window-layout blob (open tabs; UI-owned, written only when non-empty — see the Project model section). Shots carry a `starred` flag (`set_shot_starred`) that — unlike other shot edits — **writes through** to a `<assets>/shot_stars.json` sidecar (`_write_shot_stars_file`/`_load_shot_stars`), so a star persists instantly with the same timing as a take's star, without flushing buffered shot edits or marking the project dirty. The star is no longer serialized into the `.animproj`; a legacy `.animproj` still carrying `starred:true` migrates into the sidecar on load (sidecar authoritative thereafter) — and a sidecar that is **present but unreadable** falls through to that same migration path (re-materialized from the in-memory legacy flags), because the next ordinary Save strips `starred` from the `.animproj`, so leaving a corrupt sidecar in place would silently lose those stars for good (card #55). A Take records `created` (queued) **and `started`** (stamped at the GENERATING transition in `jobs.GenerationJob.run`), so the Queue tab's "done in X" shows render duration (`started`→`completed`) not queue wait — additive field, `None` for pre-existing takes (falls back to `created`). RLock-guarded; atomic JSON writes |
| `backends/replicate_client.py` | hosted generation (refactor of Fighter's `run_replicate.py`). `get_input_schema` **inlines enum `$ref`s** (`_resolve_enums`/`_follow_enum`/`_deref`): Replicate stores a property's allowed values as a `$ref`/`allOf`/`anyOf`/`oneOf` into `components.schemas`, not inline, so the resolver pulls each referenced `enum` (+ `type`) onto the property before returning — without this the editor never sees live options and silently falls back to the authored lists. `run_prediction`/`generate` take an `on_submit(pred_id)` callback (mirrors comfy's) that fires right after the create POST so the take records `backend_job_id` mid-render and becomes cancellable; `cancel_prediction(pred_id)` POSTs `/predictions/{id}/cancel` (best-effort, stops spend) |
| `backends/comfy_client.py` | local ComfyUI generation (node-role mapping); also server lifecycle/status/preflight: `launch_server` (tracks the Popen in `_server_proc`), `stop_work` (interrupt+clear queue), `stop_server` (terminate ours, else kill by port via `_pid_on_port`/`_kill_pid`), `server_status`, `monitor_snapshot`, `list_models`, `build_launch_command`, `preflight`, `dynamic_vram_enabled`/`async_offload_enabled` (preflight refuses a server using *either* PCIe weight-streaming path — see rule #10); plus crash-recovery lifecycle: `wait_until_responsive` (poll `server_status` until the server answers), `restart_server` (stop -> `launch_server` -> wait, used after a mid-render crash), and `ensure_server` (launch + wait if the server is *down*, no-op if up — the cold-start path called once before a local render, distinct from the mid-render `restart_server`). During a render `submit()` opens a **best-effort progress WebSocket** (`/ws`) on a daemon thread and feeds per-step fractions to the UI; `progress_fraction()` (pure, headless-testable) maps `progress`/`progress_state` `value`/`max` → a 0..1 fraction |
| `backends/batch.py` | overnight **batch render** pure helpers (no Qt; dependency-injected, headless-testable): `plan_batch` (eligibility filter — unknown model / invalid aspect / no start frame skipped — + the `confirm_launch` item list, **N per eligible shot**, enqueued round-major via `queue_order`), `BatchRun` (in-memory tracker; done when every take is terminal — done/failed/cancelled, which also covers 3-strike abandon), `build_batch_report` (morning summary), `sleep_command` (OS suspend argv), `POWER_NONE`/`POWER_STOP_COMFY`/`POWER_SLEEP`. The Qt side (queueing, the single cost gate, drain reaction, power action) lives in `main_window.start_batch`/`_finalize_batch`/`_perform_power_action`; the dialog is `ui/batch_dialog.py` |
| `backends/restart.py` | **restart cancelled takes** pure planner (no Qt; headless-testable): `plan_restart(takes, ...)` splits cancelled takes into `restartable` (exact-replay-from-snapshot) vs `unrestartable` (`(take, reason)`) + the `confirm_launch` item list. A take is restartable iff its snapshot is new-format (`_has_framing` = both `canvas` and `crop` keys, added 2026-06-17), its model is still in the roster, and its start keyframe still exists; otherwise unrestartable with a reason (caller marks it FAILED). The Qt side (gate, in-place flip, mark-failed) lives in `main_window.restart_cancelled_takes`/`_restart_takes`/`_restart_in_place`/`_shot_from_snapshot` — see rule #17 |
| `backends/crash_recovery.py` | `run_with_crash_recovery` — wraps one local render with crash detection + auto-restart + 3-strike retry (pure / dependency-injected so it's headless-testable): a failure with ComfyUI **down** is a crash (restart the server, retry the same take in place), a failure with it **up** is a genuine workflow error (propagates, fails only that take); on the **final (`MAX_ATTEMPTS`-th) crash it tries one last `restart_server()` + probe before giving up** (card #61) — raises `QueueAbandoned` (caller pauses the local queue) only if that restart fails or the server stays unreachable, else re-raises so just that take fails and the queue keeps running. `format_elapsed` (pure) for the "failed in XmYs" note |
| `backends/jobs.py` | `JobManager` on QThreadPool; hosted parallel, local serialized; status signals; `cancel_pending`/`pending_count`; **`cancel_shot_takes(shot_id)`** (cancel just one shot's queued takes) + **`request_stop(take_id)`** (stop an in-flight GENERATING render — comfy `stop_work` / replicate `cancel_prediction`, best-effort; flags the take in `_stopping` so the worker's unwinding backend error records CANCELLED, not FAILED; **`is_stop_requested(take_id)`** lets the hosted runner's `on_submit` self-cancel a prediction whose create-POST returned only after the stop was requested — closing the window where `request_stop` skipped the cancel because `backend_job_id` wasn't recorded yet) + **`abandon_local(reason)`** (pause the local queue after repeated crashes — clears the local pool + cancels still-pending *comfyui* takes, hosted untouched, emits **`queue_abandoned`**) + **`pause_local`/`resume_local`/`is_local_paused`** + **`stop_and_requeue`** (pause/resume the local queue for a batch — rule #16; holds queued local takes as PENDING, keeps their runners in `_runners` for re-enqueue) + **`restart_take`** (re-enqueue a CANCELLED take being restarted in place — clears its stale `_cancelled`/`_stopping`/`_requeue` membership first so the worker doesn't bail, then `enqueue`s; rule #17); `set_project` to switch the active project. Worker threads call `project.update_take` (write-through). Emits **`progress_pct(take_id, fraction, label)`** (ephemeral, UI-only — never persisted) alongside the free-text `progress` signal; the runner callback is widened to `progress(line)` for milestones / `progress(frac=.., label=..)` for the step fraction. **Every signal emit inside `GenerationJob.run` MUST go through the guarded `_emit()` helper (card #48)** — a worker thread that emits after its `_JobSignals` C++ object was deleted (project/JobManager churn mid-render) raises `RuntimeError: Signal source has been deleted`, and since `run()` is a QRunnable override invoked from C++ that uncaught exception aborts the whole process (std::terminate, no Python traceback). `_emit` (shiboken6.isValid + try/except RuntimeError) degrades a dead source to a dropped signal; `run()` also wraps its whole body so nothing escapes the override (and still fires `done_cb` so the queue slot is freed). Don't add a raw `self.signals.x.emit()` |
| `pipeline/framing.py` | `normalize_keypose` (contract framer) + `canvas_size(aspect, local=)` (hosted: longest side 1254; local: ~410k-px budget snapped to /16) + `render_keyposes(shot, dir)` (keys each keyframe sprite and places it `{scale,cx,cy}` on the aspect canvas at **generation time**). `keyed_sprite(crop_to_content=True)` crops to the foreground bbox; placement/generation pass `crop_to_content=False` so the **full original frame** is keyed transparent and `scale` is relative to the **original image height**, not the cutout (two to-scale source frames at the same scale render at the same size) |
| `pipeline/extract.py` | frame extraction + thumbnails (PyAV) |
| `pipeline/export.py` | `export_takes` → `<name>_<timestamp>/` frames + `settings.txt` |
| `pipeline/gif_export.py` | take → **animated GIF** (pure / Qt-free, headless-testable): `encode_gif(frames, out, fps, *, loop=0, max_side=None)` writes an animated GIF via Pillow (`save_all`, per-frame `duration` from fps, `loop=0`, `disposal=1` full-frame repaints); `take_to_gif(source, out, ...)` decodes a take's video with `extract.iter_frames` (PyAV → PIL) and delegates. Backs the take player's right-click *Save as GIF…* / *Copy GIF to clipboard* (rule #3) |
| `pipeline/takes_io.py` | bin / restore (only files under the project's `.assets/`; external refs left in place) |
| `ui/main_window.py` | shot cards + global filters (model + two star filters: **Starred takes** = shots with ≥1 starred take, `starredFilter`; **Starred shots** = shots the user starred, `starredShotsFilter`) + Generate/Export + **Cancel pending** + a **Restart cancelled takes** action (`restart_cancelled_takes`; enabled when the project has ≥1 cancelled take — rule #17) and a right-aligned **Full set** total-price label (`_refresh_total_price`: sums `estimate_cost` over *all* shots, ignoring the view filters; unknown-rate shots tallied as `(+N unknown)`) (the queue actions **Pause batch** / **Cancel pending** / **Restart interrupted takes** are created here via `_build_queue_actions` but render in the **Queue** tab header, not the Shots strip; the Shots-tab control strip keeps the filters, Export view, Generate batch and the Full-set price); **File** menu project lifecycle (**New/Open/Save/Save As** + **New Shot**) with dirty-marker title (`*` when the project is untitled, has buffered edits, or any open shot tab has uncommitted edits — `_has_unsaved_changes`) + save-prompt; a **Settings** menu with a checkable **Update Replicate model data on startup** (persisted via `store/app_settings.py`; when on, `_maybe_refresh_schemas_on_startup` kicks off the Model Library tab's off-thread schema fetch at launch); **closable** tabbed central widget (Shots / Assets / Model Library / ComfyUI Status; reopen from **View**). Double-click a shot row (or **+ New Shot**) opens a shot tab; shot tabs tracked in `shot_tabs`. The open-tab layout (which fixed/shot/take tabs, order, active) is captured into `project.ui_state` on save (`_capture_tab_state`) and rebuilt on open (`_restore_tab_state`) — see the Project model section |
| `ui/comfy_monitor_window.py` | the **ComfyUI Status** tab: status/version, RAM+VRAM, queue, launch settings, installed models + **Launch ComfyUI**/Stop working/Shut down controls (the latter two emit `stop_intent` so MainWindow pauses an active batch first — rule #16); `start_monitoring`/`stop_monitoring` poll only while the tab is visible (off-thread `_MonitorPoller` + `_AsyncCall` for stop/shutdown, same closed-port-timeout reason) |
| `ui/shot_card.py` `ui/takes_view.py` | shot row (double-click opens its shot tab; Generate / Export; a **★/☆ star toggle** in the header (`star_btn`/`_refresh_star_btn`, emits `star_toggled`); **right-click context menu** — Edit / Generate / Duplicate / Star (Star/Unstar) / Delete, built in `_build_context_menu` so it's headless-testable without `exec()`) + inline takes folder grid. `takes_view` likewise builds its per-take right-click menu in `_build_context_menu(ids)` (no `exec()`); a **Restart take** entry appears when the selection holds a cancelled take **or a crash-interrupted FAILED take** (card #64) and emits `restart_requested` up to MainWindow (rule #17). Each **take** thumbnail also carries a **clickable ★/☆ star badge** (top-left, painted + hit-tested by `_StarDelegate` via the pure `star_badge_rect`; a click toggles the take's star write-through, mirroring the shot-card star and the right-click "Toggle star" — the star shows as this badge, not prefixed into the tile label). `TakesView` takes an optional `jobs` (the `JobManager`, threaded through `ShotCard`/`ShotTab`): when given, it subscribes to `progress_pct` and shows a **generating take's live render %** in its tile label (`▶  NN%`, the same number the Queue tab reports), updated in place. Hosted takes expose no native % (the Queue shows a busy bar) so they stay `▶  generating`. Pure label helpers `progress_percent`/`take_tile_label` are smoke-tested in `smoke_phase4`. **A take's status signal refreshes incrementally, not by full reload (card #75):** `jobs.status_changed`/`finished` → `main_window._refresh_shot_for_take` → `ShotCard`/`ShotTab.update_take` → `TakesView.update_take(take_id)`, which updates just that take's `QStandardItem` (badge/%/star/thumbnail) in place instead of `load()`'s `model.clear()`+rebuild + every-thumbnail/strip re-decode — so a shot accumulating N takes during a batch no longer does O(N) item+icon+strip work (and twice, on the done+finished double-fire) per transition. It falls back to `load()` only when view membership actually changed (a not-yet-shown take, a deletion, a Favorites-filter boundary); a finished take gets a **single-take** strip decode (`_strip_pending`-deduped) rather than re-decoding the whole grid. `_icon_for` is QIcon-cached per take by a content signature (thumbnail path+mtime, else status placeholder). `load()` stays for the initial fill / shot switch and for the discrete `_refresh_shot` (queue/restart/delete) path; `reload()` still full-rebuilds all cards on discrete actions. Smoke-tested in `smoke_phase4.test_takes_view_incremental_update` |
| `ui/shot_tab.py` | the **shot tab**: full editor + the shot's takes grid + Save/Generate/Export. Tab title shows a trailing `*` while the editor has uncommitted edits (`is_dirty`/`dirty_changed`; cleared on `commit()`, suppressed during load). Per-model **Aspect** dropdown (turns red + blocks Generate if invalid for the model); per-keyframe placement: left-click a keyframe to frame it, double-click to pick. **Prompt** subtab: **Load template** / **Save as template** buttons (backed by `store/prompt_library.py`; Load pops a picker of saved prefabs, Save stores the current prompt under a name) above the positive/negative boxes; the **Negative** box is greyed + disabled for models whose live schema has no `negative_prompt` field, its text replaced by a "does not support negative prompting" placeholder — the real text is **stashed** (`_neg_stash`) and restored when a supporting model is reselected, so it's never shown as a stale value nor silently lost (`_negative_supported`/`_refresh_negative_state`; all negative reads/writes route through `_negative_value`/`_set_negative`). Left editable when the schema's unfetched, since the backend drops an unsupported negative anyway |
| `ui/placement_widget.py` | the framing canvas: drag a keyed sprite to position + corner-handle scale on the magenta aspect canvas, plus an **editable readout panel** (X/Y px, W/H px, W%/H% spin boxes) for precise numeric entry — all linked views of the placement; placement stored normalized `{scale,cx,cy}` |
| `ui/asset_picker.py` | the visual keyframe picker dialog (thumbnail grid + Import) |
| `ui/assets_view.py` | the **Assets** tab: drag-drop / Import keyframe images into `.assets/`; thumbnail grid + delete |
| `ui/cost_confirm.py` | the launch gate (`confirm_launch(items: list)` — takes a **batch**, shown as one summary; the overnight batch uses this to confirm the whole run once) |
| `ui/batch_dialog.py` | the **Generate batch…** dialog (Shots-tab control strip): scope (all shots / current view), takes-per-shot, and the when-finished power action (nothing / stop ComfyUI / sleep PC). Thin Qt over `backends/batch.py` |
| `ui/model_library_window.py` | the **Model Library** tab: model roster + a **Refresh from Replicate** button (off-thread `_SchemaFetcher`) that pulls every Replicate model's input schema into `store/schema_cache.py` AND derives + syncs its capability flags into `model_library.json` (`library.sync_model_capabilities`); a **Schema** column shows the cached field count and a **Capabilities** column shows the synced negative/fixed-camera flags. Pricing isn't API-exposed, so costs are left alone |
| `store/schema_cache.py` | persistent cache of Replicate input schemas (`data/schema_cache.json`, keyed by `replicate_model_id`); lock-guarded, atomic writes (reuses `store.project._atomic_write_json`). Populated by the Model Library tab; read by the shot editor for per-param enums/types **and to decide whether a model accepts a negative prompt** |
| `store/app_settings.py` | app-global user preferences (`data/app_settings.json`; `get_bool`/`set_bool`). Own file (NOT `app_state.json`, which `main_window._remember_last` rewrites wholesale). Same lock + atomic-write discipline as `schema_cache`. First key `update_schemas_on_startup` (default off) — read by `MainWindow` to auto-refresh Replicate schemas at launch, toggled from the **Settings** menu |
| `store/prompt_library.py` | app-global library of reusable prompt prefabs (`data/prompt_templates.json`; entry = `{name, positive, negative}`, upsert-by-name). Same lock + atomic-write discipline as `schema_cache`; ships seed templates; read/written by the shot tab's Prompt subtab template combo |
| `remote/` | opt-in localhost control server so an external agent (Claude) can drive the live GUI like a web page — `server.py` (`RemoteControlServer`: `ThreadingHTTPServer` on 127.0.0.1, endpoints `/health` `/snapshot` `/screenshot` `/click` `/type` `/key` `/set`), `bridge.py` (`GuiBridge`: marshals each widget touch onto the GUI thread via a posted `QEvent`, so it never races the event loop and still works while a modal is open), `snapshot.py` (pure, headless-testable: `build_snapshot`/`resolve_target` + action primitives `do_click`/`do_type`/`do_key`/`do_set`/`grab_png`). Off unless `ANIMGEN_REMOTE` is truthy; `MainWindow._maybe_start_remote` starts it, `closeEvent` stops it |
| `scripts/` | `seed_configs.py` (writes `Fighter.animproj`, imports keyframes as assets) + `smoke_phase*.py` + `remote_cli.py` (stdlib client for the `remote/` control server) + `launch_comfyui.py`/`.bat` (local backend, `--disable-dynamic-vram`) + **`mock_comfy.py`** (the supported **offline GPU-free** fake ComfyUI — drive real local batches with no GPU; see "Offline GPU-free harness" under Run / test / seed). `_`-prefixed scripts are dev/diagnostic scratch (the rule #18 crash hunt: `_app_watch.py`/`_crash_watch.py`/`_widget_census.py`/…) |
| `data/` | runtime (gitignored): `*.animproj` project files (default `Fighter.animproj`) + their sidecar `<name>.assets/` (flat keyframe images + `takes/`, `thumbs/`, `.bin/`); plus `exports/`, `_scratch/` (untitled-project assets), `app_state.json` (last opened) |
| `workflows/` | bundled ComfyUI templates for the local backend |

## Project model (file-based, 2026-06-16)

- A project is a `Foo.animproj` JSON file (`{format, version, name, shots:[...]}`,
  authoring data only — plus an optional `ui_state` window-layout blob, see below) + a
  sidecar `Foo.assets/` folder beside it holding `takes.json` (take metadata),
  `shot_stars.json` (write-through shot stars), the flat keyframe **asset** images, and
  managed media (`takes/`, `thumbs/`, `.bin/`).
- **Open-tab layout (`ui_state`, 2026-06-18):** the `.animproj` doc carries an optional
  `ui_state = {"tabs": [...], "active": int}` recording which tabs were open (and their
  order) so reopening a project restores its layout (`active` is the index into the `tabs`
  descriptor list — not a raw tab position — so it survives a later tab being skipped on
  restore, and the active tab is re-selected by identity). Each tab descriptor is `{"kind":
  "fixed", "key": <title>}` (a closable fixed tab — Shots/Queue/Assets/Model Library/
  ComfyUI Status), `{"kind": "shot", "id": <shot_id>}`, or `{"kind": "take", "id":
  <take_id>}`. It's **UI-owned window metadata, not authoring data**: `Project.ui_state`
  is a plain dict the MainWindow fills in, it does **not** set `dirty`, and it's
  **captured at save time** (`_capture_tab_state` in `save_project`/`save_project_as`,
  right after `_commit_open_shot_tabs`) and **restored on open** (`_restore_tab_state` in
  `__init__` + `_switch_project`, after `reload()`). Additive/back-compatible: omitted
  from the doc when empty; a missing key (older/seeded files) falls back to the default
  full fixed-tab set. Restore reopens shot/take tabs by id via `open_shot`/`open_take`,
  which silently skip an id whose shot/take was since deleted. A pure tab rearrange on an
  otherwise-clean project is NOT an unsaved edit and won't arm the save-prompt. **It IS,
  however, persisted at window close (card #50):** `closeEvent` → `_persist_layout_on_close`
  records the layout even on a no-save close, but **only on the genuinely-clean path** —
  titled project, and **no unsaved authoring edits** (`_has_unsaved_edits` checked *before*
  `_maybe_save_changes`). It's skipped on Discard (writing would serialize the just-discarded
  in-memory shots back to disk) and on untitled (nowhere to write without a Save-As prompt),
  and it's gated on the layout *actually differing* from disk — `_compute_tab_state()` vs the
  effective on-disk state (`project.ui_state`, or `_default_tab_state()` when empty) — so an
  unchanged close never touches the `.animproj` mtime. The write goes through
  `Project.persist_ui_state()`, which rewrites **only the `.animproj`** (not `takes.json`) and
  does **not** clear `dirty`, keeping it out of the authoring save contract. Smoke-tested in
  `smoke_phase5.test_tab_state_persistence` + `test_tab_state_persists_on_close`.
- **Assets & framing:** keyframe images live flat in the `.assets/` root (folder-scanned).
  Drag images into the **Assets** tab (or Import) to `import_asset` them (copied in,
  originals untouched). A shot's `start_frame`/`end_frame` point at assets; `shot.crop` is
  `{aspect, start:{scale,cx,cy}, end:{...}}`. The canvas **aspect** (a per-model dropdown)
  drives the canvas size (hosted: longest side 1254; local: a ~410k-px budget snapped to
  /16, written into the Wan workflow via `comfy_nodes.size_node` so wide/tall aspects
  render wide/tall). Each keyframe sprite is keyed (whole frame, transparent background) +
  drag/scale-placed on that canvas at generation time; `scale` is the **original image
  height** as a fraction of the canvas (not the cutout), so two source frames drawn to-scale
  with each other render at the same size when given the same scale. No baked keypose files /
  no `keyposes/<hash>/` folders. **Migration note (2026-06-17):** `scale` switched from
  cutout-relative to original-relative with no data migration — the stored number is
  reinterpreted, so shots authored before this change render the character a bit smaller
  (original frame height ≥ cutout height) and may need re-framing. Existing takes are
  unaffected (their `settings_snapshot` framing is frozen); the seeder writes no custom crop,
  so seeded shots only pick up the new default semantics.
- **Hybrid persistence:** authoring edits (add/rename/delete shots, prompts, framing)
  buffer in memory and set `dirty` (title shows `*`, prompt before discarding); a
  **completed Take auto-persists immediately** to `takes.json`. The split (shots in the
  `.animproj`, takes in `takes.json`) is what lets a finished render persist without
  flushing buffered shot edits. **Shot stars are the one authoring field that also writes
  through** (to `shot_stars.json`), so star/unstar — of a shot *or* a take — persists
  instantly without a Save and without marking the project dirty. Note shot-tab editor edits set the tab's own dirty flag
  *before* they're committed to the buffer, so an open-but-unsaved shot tab shows `*` on
  its tab and contributes to the project title `*` even while `project.dirty` is still False.
- **Paths:** managed media + assets serialize **relative to `assets_dir`**; external
  references (seeded `../Fighter/out/*.gif`/`.mp4` takes) stay **absolute**. Untitled
  projects keep assets in `data/_scratch/<id>/` until the first **Save As**, which
  relocates them next to the chosen file. **`save_as` is atomic against a failed document
  write:** it moves scratch (untitled) / copies assets (saved) + remaps in-memory paths +
  swaps identity (`path`/`name`/`_assets_dir`) and only commits once `save()` succeeds; if
  the write raises (e.g. `_atomic_write_json` exhausts its Windows AV/indexer retries) it
  rolls everything back — identity restored, remap reversed, the moved scratch moved back
  (so untitled work is never lost), the copy/partial `.animproj` dropped. A Save-As **over
  an occupied target** moves that neighbour's existing `.assets` sidecar *aside* (not
  `rmtree`) and restores it on failure, so a failed overwrite leaves both this project and
  the neighbour exactly as they were. Smoke-tested in
  `smoke_phase5.test_save_as_rollback_on_write_failure`.
- **Generate** saves the project first (untitled → Save As prompt) so a take never
  references an unsaved shot.

## External wiring (overridable env vars)

- `ANIMGEN_FIGHTER_ROOT` (default `../Fighter`) — keypose assets + the seed manifest.
- `ANIMGEN_COMFY_DIR` (default `../comfyui`) — local ComfyUI (input/output dirs).
- **`REPLICATE_TOKEN`** — read from the environment, then a repo-local `.env`, then
  the source project's `.env`. The token is **never committed** (`.env` is gitignored).
- `ANIMGEN_REMOTE` (default off) — when truthy, start the localhost control server that
  lets Claude drive the GUI (see `remote/`). `ANIMGEN_REMOTE_PORT` (default 8765; 0 =
  ephemeral) sets its port. Localhost-only, no auth — a dev/automation aid, not for prod.
- `ANIMGEN_NO_WS_PROGRESS` (default off) — when truthy, disable the best-effort local-render
  progress WebSocket (`comfy_client._start_progress_ws`). Renders still complete via the
  `/history` poll (rule #11); you only lose the live % bar. It's both an escape hatch and a
  **bisection lever** for the 2026-06-18 native stack-overflow crash investigation (rule #18):
  run a batch with it off — if the crash stops, the WS is the culprit; if it still crashes, the
  WS is exonerated and the overflow is in Qt/C++ on the main thread.

## Hard-won rules / gotchas

1. **Cost-confirm gate before EVERY launch** (hosted or local). The dialog defaults to
   Cancel. Don't bypass it. Mirrors the source project's "ask before every generation".
2. **Additive — copy in, never move external originals.** Importing a keyframe asset
   COPIES it into `.assets/` (deliberate; the original is left untouched). Delete-to-bin
   only moves files under the project's `.assets/`; a take pointing at an external file
   (e.g. a seeded `../Fighter/out/` gif) is flagged deleted but left in place. Never
   relocate/delete anything outside the project.
3. **Each take stores an immutable `settings_snapshot`** — frozen at launch
   (`main_window.generate_shot`). This is the whole point (the source project had no
   per-take metadata). Don't mutate it. It captures model/backend/replicate id/workflow,
   start+end frames, prompt+negative, the resolved `settings` dict, **and the framing**
   (`canvas` `[w,h]` + `crop` aspect/placement — added 2026-06-17 so re-framing a shot
   post-generation can't silently change a take's recorded canvas/aspect). `export.py`
   writes the snapshot verbatim into `settings.txt`, and the take viewer
   (`ui/take_player.py`) surfaces it on demand: a **⚙ Settings** button next to the frame
   timer and a right-click **Show generation settings** on the video both reveal a
   dockable panel (a floatable `QDockWidget` in an inner `QMainWindow`) rendered by the
   pure, headless-testable `format_generation_settings(take, shot=None)`. The same
   right-click menu also offers **Save as GIF…** (file dialog → `pipeline.gif_export.take_to_gif`)
   and **Copy GIF to clipboard** (encode to a stable temp `.gif` under
   `tempfile.gettempdir()/animgen_gif_clip/<take>.gif`, then a file-URL `QMimeData` →
   CF_HDROP, so paste carries the *animated* file into a chat/email/Explorer — user's call,
   file-only, not a static bitmap); both are gated on a playable source, encode off the GUI
   thread via `_GifExporter`, and are smoke-tested in `smoke_phase5` (`test_gif_export` +
   `test_take_player_gif_export`). **The render also
   reads FROM the snapshot, not the live Shot (card #53):** `_queue_take` feeds `_make_runner`
   a snapshot-derived synth Shot (`_shot_from_snapshot`, the same helper the restart path
   uses), so a shot edited+saved before the serialized worker (or a later batch round)
   dequeues can't make the take render a different prompt/framing/canvas than its own
   snapshot records. The worker-thread closures (`framing.render_keyposes` + both backends'
   `generate`) must keep reading off that synth Shot — never re-close over the live `shot`.
   `_shot_from_snapshot` deep-copies the snapshot's `crop`/`settings` into the synth so a
   reader can't mutate the frozen snapshot through a shared dict.
4. **Smoke tests run headless** with `QT_QPA_PLATFORM=offscreen`; never call a modal's
   `.exec()` in a test (it blocks). Tests override `paths.SCRATCH_DIR` to a tempdir so
   untitled-project scratch stays out of `data/`. `build_summary` / pure functions are
   split out for exactly this reason.
5. **`model_library.json` is mostly authored, with two auto-synced capability flags.**
   IDs, costs, notes, aspect_ratios, and `supports_end_frame` are hand-authored
   (Replicate's API exposes **NO** pricing — costs are scraped from the web pricing page,
   so the refresh can't and doesn't touch them; `supports_end_frame` predates the sync and
   a roster invariant relies on it). The two **capability flags** `supports_negative_prompt`
   and `supports_camera_fixed` ARE auto-derived from the live input schema and written back
   into `model_library.json` by the **Model Library** tab's *Refresh from Replicate* button
   (`library.sync_model_capabilities`, derived via `replicate_client.derive_capabilities`)
   — don't hand-edit those two, a refresh overwrites them. They're a roster record shown in
   the Model Library tab's Capabilities column; the shot editor still reads negative-prompt
   support live from the schema cache, not from this flag. Per-param schemas are fetched
   live and cached to `data/schema_cache.json` (`store/schema_cache.py`, keyed by
   `replicate_model_id`), read from there by the shot editor — the editor no longer fetches
   per shot. `get_input_schema` **resolves enum `$ref`/`allOf`/`anyOf`/`oneOf` into inline
   `enum`s** at fetch time, so the cached schema carries live option lists and the shot
   editor reflects Replicate's current resolution/duration/mode values automatically; the
   authored `resolution_options`/`duration_range`/`mode_options` are a fallback for when the
   live fetch hasn't run or failed. The same refresh path (`start_schema_fetch`) backs both
   the *Refresh from Replicate* button and an opt-in launch-time refresh via **Settings →
   Update Replicate model data on startup** (default off; `store/app_settings.py`) — so a
   startup refresh also re-syncs the capability flags. Note: a refresh rewrites the file via
   `_atomic_write_json` (indent=2), so the roster is stored in that normalized format.
6. **Windows/MINGW:** use `python` (not `python3`); set `PYTHONIOENCODING=utf-8`.
   `rm -rf` is guarded — don't rely on it for cleanup. Pass Windows-style paths
   (`C:/...`) to `sys.path.insert`, not MINGW (`/c/...`) paths.
7. **Secrets:** never copy `.env` into the repo tree (it's destined for a public repo).
   The runtime token fallback exists precisely so we don't have to.
8. Pyright may flag `from store... / from ui...` imports as unresolved — false
   positives (the repo root is on `sys.path` at runtime; `pyrightconfig.json` sets the
   venv). Runtime is the source of truth; smoke tests gate it.
9. **Take persistence is write-through and runs off worker threads** — keep all project
   JSON writes serialized under the `RLock` and use unique temp names (see
   `store.project._atomic_write_json`). Two takes finishing at once will otherwise race
   `os.replace` on Windows (`WinError 32`); the atomic-write helper holds the lock across
   build+write and retries briefly for AV/indexer locks.
10. **Local backend MUST disable BOTH PCIe weight-streaming paths
    (`--disable-dynamic-vram --disable-async-offload`).** ComfyUI streams 14B weights
    RAM↔VRAM mid-kernel over PCIe, and on the 12GB card a single op can stall past Windows'
    2s GPU watchdog (TDR) → driver reset → server dies mid-job, no traceback (Fighter, a
    night of crashes 2026-06-14; AnimGen overnight-batch kill 2026-06-17; see
    `../Fighter/research/comfyui-gpu-watchdog-crash-and-aimdo.md`). There are **two
    independent** streaming engines and disabling one is not enough: **dynamic VRAM**
    (the `comfy-aimdo` engine, gated by `enables_dynamic_vram()`, off via
    `--disable-dynamic-vram`/`--highvram`/`--gpu-only`/`--novram`/`--cpu`) **and async
    weight offloading** (`--async-offload`, a *separate* path **default-on on Nvidia/AMD**,
    `NUM_STREAMS=2`, NOT covered by `--disable-dynamic-vram` — off only via
    `--disable-async-offload`/`--cpu`/`--async-offload 0`). The 2026-06-17 kill happened with
    aimdo correctly off but async offload still on ("Using async weight offloading with 2
    streams"). AnimGen doesn't start ComfyUI, so `comfy_client.preflight()` reads the launch
    `argv` (+ device type) from `/system_stats` and **refuses to submit a local job** if
    *either* path is active (`dynamic_vram_enabled()` / `async_offload_enabled()`; `/system_stats`
    exposes no direct aimdo field, so detection stays argv-derived). Start ComfyUI with both
    flags via the **Launch ComfyUI** button in the **ComfyUI Status** tab (detached, logs to
    `data/comfyui_server.log`) or `scripts/launch_comfyui.py`/`.bat` — `build_launch_command()`
    is the source of truth (`REQUIRED_FLAGS`); the `.bat` hardcodes the same flags to match.
    Escape hatch: `ANIMGEN_ALLOW_DYNAMIC_VRAM=1` (bypasses the whole guard).
    Note: probing a *down* localhost port costs a full socket timeout on this machine (SYNs
    to closed ports are dropped, not refused), so the **ComfyUI Status** tab polls on a
    daemon thread (`_MonitorPoller`, started only while that tab is visible) and never
    blocks the GUI thread on `server_status()`.
11. **Local-render progress comes over ComfyUI's WebSocket, best-effort.** ComfyUI exposes
    true per-step progress only on `/ws` (`progress` / `progress_state` messages with
    `value`/`max`) — there's no HTTP progress endpoint. `comfy_client.submit()` opens
    `ws://…/ws?clientId=…` on a daemon thread and translates each message into a 0..1
    fraction via `progress_fraction()` (pure → unit-tested in `smoke_phase2`), surfaced as a
    determinate **% bar** in the Queue tab (`ui/queue_view.py`) **and as a `▶ NN%` label on
    the generating take's tile** in its shot's takes grid (`ui/takes_view.py`, which subscribes
    to the same `progress_pct` signal when handed the `JobManager`). It is **non-fatal**: any WS
    failure is swallowed and the `/history` poll still drives the render to completion —
    and `/history` (not the last WS message) stays the authoritative *done* signal, because
    a documented `progress_state` tail keeps arriving ~20–30s after completion. Requires
    `websocket-client` (in `requirements.txt`). **Hosted (Replicate) exposes no native
    %** — progress lives only as free text in `logs` — so the Queue tab shows an
    indeterminate *busy* bar labelled with the elapsed-time line `run_prediction` already
    emits, rather than a percentage.
12. **Local renders auto-recover from a ComfyUI crash; 3 strikes pauses the queue.** A 14B
    render can still occasionally trip the 2s GPU watchdog (TDR) even with
    `--disable-dynamic-vram`, killing the ComfyUI *process* mid-render. **First, though, the
    comfyui runner cold-starts ComfyUI if it isn't running at all** — `_make_runner` calls
    `comfy_client.ensure_server(progress_cb=...)` *before* the crash-recovery loop, so firing a
    local Generate with the server down launches it (with the safe flags via
    `build_launch_command`) as an honest "starting ComfyUI" step rather than letting the
    server-down failure get misread as a crash and burn a retry attempt. The local pool is
    serialized, so the first queued take starts the server and the rest find it up; a genuine
    start failure (install missing / port won't bind) raises straight out of the runner and
    fails just that take. The comfyui runner is then
    wrapped in `crash_recovery.run_with_crash_recovery` (`ui/main_window._make_runner`): the
    **crash signal is "the server is down at the moment the render failed"** (`server_status()`)
    — a failure with the server still *up* is a genuine workflow error and propagates, failing
    only that take. The "down" reading is **reconfirmed up to `CRASH_PROBES` (3) times**
    (`_looks_crashed`) before committing to a restart so a transient `/system_stats` blip on a
    still-alive server isn't misread as a crash; the *first* "up" is trusted immediately, so the
    common workflow-error path still probes exactly once (no slowdown), and on a genuinely down
    server each probe already eats a full socket timeout so the reconfirmation needs no sleep.
    On a crash it `restart_server()`s (which relaunches with the safe flags via
    `build_launch_command`, fixing the root cause) and retries the **same** take *in place*.
    `restart_server` waits `RESTART_SETTLE_S` (2s) after the kill so the OS releases `COMFY_PORT`
    before the relaunch rebinds it, and watches the relaunched process (`proc.poll()` via
    `wait_until_responsive(is_alive=...)`) so a bind loss / immediate exit fails fast instead of
    stalling the full `ready_timeout_s` (120s).
    Because the local pool is serialized, retrying in the blocked worker makes "requeue the
    rest" automatic — the other queued takes just wait behind it; nothing is re-enqueued. On the
    **final (`MAX_ATTEMPTS`-th, 3rd) crash the queue is NOT abandoned on attempt count alone**
    (card #61): a transient GPU drop can crash all three attempts yet still recover with one more
    restart, so the last strike first tries **one final `restart_server()` + responsiveness probe**
    (`_looks_crashed`). The queue is abandoned (`raise QueueAbandoned` -> `jobs.abandon_local()`
    cancels the remaining **local** pending takes, hosted untouched, `queue_abandoned` surfaces a
    "queue paused" warning) **only if that final restart raises or the server stays unreachable** —
    so a legitimately-broken GPU can't trigger a restart loop, but a recoverable one isn't condemned
    on count alone. If the final restart brings ComfyUI back, the render error is **re-raised so only
    *this* take fails** and the rest of the local queue keeps rendering. (That recovered-but-failed
    take is recorded `FAILED` with **`interrupted=True`** so the bulk *Restart interrupted takes*
    action picks it up like its `abandon_local`'d siblings — card #68: `crash_recovery` stamps the
    re-raised render error with `CRASH_INTERRUPTED_ATTR` and the worker's else branch reads it via
    `getattr`, distinguishing a crash-killed take from a genuine workflow error, which stays
    `interrupted=False`; the original error message is kept verbatim.) The **queue-abandon crash
    victim is flagged the same way** (card #71): the `QueueAbandoned` raised when the final restart
    fails or the server stays unreachable is itself stamped `CRASH_INTERRUPTED_ATTR`
    (`crash_recovery._abandon`), so the worker's else branch records the take that crashed
    `MAX_ATTEMPTS` times as `FAILED + interrupted=True` too, matching the still-PENDING siblings
    `abandon_local` cancels with the same flag (so the bulk *Restart interrupted takes* picks up the
    whole abandoned batch, victim included). The retry/abandon notes
    ("failed in XmYs, retrying (attempt n/3)", "attempting a final restart…", "recovered after a
    final restart…") flow through the normal `progress(line)` path, so they show on the take in the
    Queue tab and the main log.
    Each retry re-runs `preflight()`, so the dynamic-VRAM gate is never weakened. (Distinct from
    `backends/recovery.py`, which reconciles takes orphaned by an *app* restart on load.)
    **A deliberate user stop suppresses the auto-restart** (rule #16): `run_with_crash_recovery`
    takes a `should_abort` predicate (wired to `jobs.is_local_paused`); when the user has paused
    the local queue, a render failure is treated as the intended stop and re-raised verbatim
    (no restart, no abandon) instead of being misread as a crash — otherwise pausing/shutting
    down ComfyUI mid-batch would just get undone by the restart.
13. **Claude can drive the GUI over an opt-in localhost control server** (`remote/`), so a
    desktop-app change can be verified without full-PC-control / pixel-clicking. It's the
    same model as the Chrome MCP, scoped to AnimGen: `GET /snapshot` returns a DOM-like list
    of the visible widgets (`ref`/`class`/`name`/`text`/`rect`/`enabled`), `GET /screenshot`
    returns a PNG of the window via **`QWidget.grab()`** (Qt's own compositor — no OS
    screen-capture, works even occluded), and `POST /click|/type|/key|/set` drive a widget by
    `ref` / `objectName` / visible `text`. Two non-obvious invariants: **(a)** the HTTP server
    runs on a daemon thread but every widget touch is marshalled onto the GUI thread by
    `GuiBridge.call` (a posted `QEvent`); a modal runs a nested event loop so the calls still
    land while the **cost-confirm gate** is open — the gate is *driven*, never bypassed.
    **(b)** It is **off by default** and **127.0.0.1-only, no auth** — enable per-launch with
    `ANIMGEN_REMOTE=1` (port via `ANIMGEN_REMOTE_PORT`, default 8765). Drive it with
    `scripts/remote_cli.py` or `curl`; pure helpers + a full round-trip are covered by
    `smoke_phase7`. Add an `objectName` to a control only when text/`Class:ordinal` targeting
    is ambiguous (a few exist: `mainTabs`, `modelFilter`, `starredFilter`, `starredShotsFilter`, `logDock`).
14. **Overnight batch render is one cost-confirm for the whole run, then unattended.**
    **Generate batch…** (Shots-tab control strip) queues every eligible shot × N takes after a
    **single** `confirm_launch(plan.items)` — this is how the per-launch cost gate (rule 1) is
    honored for an unattended run: the one dialog itemizes all N×shots and the full total, so no
    take fires without confirmation. Both backends (hosted parallel, local serialized as usual);
    each take reruns the random-seed roll in `_queue_take` so N takes vary. Takes are
    enqueued **round-major** (`batch.queue_order(plan.eligible, n)`): one take of every
    eligible shot per round, repeated N rounds, NOT all N takes of shot 1 before shot 2's
    first (shot-major) - so on the serialized local queue an early take of every shot lands
    before extra takes pile onto any one shot (card #51). The render side is
    already unattended-safe (crash recovery + 3-strike abandon, write-through takes). A `BatchRun`
    (in `main_window`, in-memory, **not persisted** — a mid-batch app restart falls back to ordinary
    orphan recovery) tracks the take-ids; `_on_status_changed` marks each terminal take and, once all
    drained, `_finalize_batch` writes a report to `data/exports/overnight_<ts>.txt` and runs the chosen
    **when-finished** power action: stop ComfyUI (frees the GPU) and optionally sleep the PC
    (`batch.sleep_command()`), both best-effort on a daemon thread. Pure logic (`plan_batch`,
    `BatchRun`, `build_batch_report`, `sleep_command`) is in `backends/batch.py`, smoke-tested in
    `smoke_phase2.test_batch`.
15. **AnimGen runs its own UI on software rendering so a GPU TDR can't kill it.** A driver
    reset (rule #10) is a kernel-level event that invalidates **every** GPU device context on
    the machine at once — it killed ComfyUI *and* AnimGen together (2026-06-17), so the rule-#12
    crash recovery never got to run (the orchestrator died too). `app._force_software_rendering()`
    runs **before `QApplication`** (Windows-only) and forces Qt to software OpenGL
    (`QT_OPENGL=software` + `AA_UseSoftwareOpenGL`) so the UI process holds **no GPU device
    context**. AnimGen is pure QtWidgets + **CPU (PyAV) video decode** (`ui/take_player.py`
    decodes to QImages/QPixmaps — no QtMultimedia/QtQuick/QOpenGL anywhere), so this changes
    only how its *own* window paints; the WAN render runs in ComfyUI/CUDA and is **bit-for-bit
    unaffected (zero quality impact)**. A TDR now kills only ComfyUI; AnimGen survives, sees the
    dropped render, and rule-#12 restarts+retries — overnight batches become resilient. Escape
    hatch `ANIMGEN_ALLOW_GPU_UI=1` keeps hardware GL. (Complementary to rule #10: #10 *prevents*
    the TDR, #15 *survives* it — both quality-neutral.)
16. **A running batch can be PAUSED/RESUMED, and a deliberate ComfyUI stop halts it instead of
    being auto-restarted.** Before this, stopping ComfyUI mid-batch did nothing: a *Shut down*
    killed the server, crash-recovery (rule #12) read the down server as a crash and relaunched
    it; a *Stop working* failed the current take and the serialized worker just moved to the
    next. The fix is one flag the queue and crash-recovery share: **`JobManager._local_paused`**
    (`is_local_paused()`), consulted by `run_with_crash_recovery`'s `should_abort` so a render
    failure while paused is the *intended* stop (re-raised, no restart). `pause_local(requeue_current=False)`
    sets the flag, `clear()`s the queued local runnables and returns the held take ids — left
    **PENDING, not cancelled** (so the batch can't finalize while paused → no when-done power
    action fires); the GENERATING take is left to finish, unless `requeue_current` (then
    `stop_and_requeue` interrupts it and resets it to PENDING so resume re-runs it). `resume_local(held)`
    clears the flag and re-enqueues each held take with its **original retained runner**
    (`JobManager._runners`, dropped only when a take goes terminal — kept across a requeue reset
    to PENDING) so there's no `settings_snapshot` drift. UI: a **Pause batch / Resume batch**
    toggle in the **Queue** tab header (enabled only while `self._batch` is active; the Pause
    dialog offers *pause after current* vs *halt current & re-add*), and the **ComfyUI Status**
    tab's *Stop working* / *Shut down* emit `stop_intent` → `MainWindow._pause_local_on_stop_intent`
    pauses an active batch first so the stop sticks. `held` + `paused` live on the in-memory
    `BatchRun`; `cancel_pending` clears the pause flag (aborting the whole queue). Scope: **local
    (ComfyUI) queue only** — hosted (Replicate) takes are a separate pool with no crash/restart
    issue and keep running. Pure logic is smoke-tested in `smoke_phase2` (`test_pause_resume_local`,
    `test_pause_requeue_current`, and crash_recovery case (g)).
    **The same `stop_intent` handler also covers a non-batch local queue (card #42):** a manual
    ComfyUI stop with single local takes in flight (queued *not* via *Generate batch…*) would
    otherwise be fought by crash-recovery exactly like the batch case (server down → read as a
    crash → relaunch + retry). With no batch active, `_pause_local_on_stop_intent` calls
    `jobs.pause_local()` to set the flag (so the in-flight take fails cleanly, no restart) and
    — since there is **no Resume UI** for single takes — `cancel_take`s each queued local take
    (leaving them held PENDING would either zombie the queue or re-launch the server via the
    runner's `ensure_server`). The pause is **transient**: `_on_status_changed` calls
    `jobs.clear_local_pause()` (clears the flag, re-enqueues nothing — distinct from
    `resume_local`) once `_local_work_in_flight()` reports the local queue has drained, so a
    later render recovers from a genuine crash normally and the flag can't stick True. Marked by
    `MainWindow._stop_paused_local` (reset by `cancel_pending` too). Smoke-tested in
    `smoke_phase2` (`test_clear_local_pause`, `test_stop_pauses_nonbatch_local`).
17. **Cancelled takes can be RESTARTED — exact-snapshot replay in place, else marked failed (card #49).**
    A cancelled take (esp. the batch of PENDING takes orphan recovery cancels after a mid-batch
    crash) carries its immutable `settings_snapshot` (rule #3), which — for takes made on/after
    2026-06-17 — records the framing (`canvas`+`crop`) alongside model/frames/prompt/settings/seed.
    So a restart rebuilds the runner straight from the snapshot (same seed, same framing) and re-runs
    the take **in place** (flip CANCELLED→PENDING, reset only its output/timing fields, re-enqueue via
    `jobs.restart_take`) — the snapshot is never mutated. `main_window._shot_from_snapshot` wraps the
    snapshot in a throwaway `Shot` fed to the existing `_make_runner` + `framing.render_keyposes`, so
    local restarts reuse the unchanged `ensure_server`/`preflight`/crash-recovery path (rules #10/#12).
    A take that **can't** be replayed exactly (its snapshot predates framing-in-snapshot, its model
    left the roster, or its start keyframe is gone) is **marked FAILED with a reason** — there is no
    fresh re-Generate/reroll fallback (deliberate, user's call 2026-06-18: "exact restart in place, if
    assets are no longer available then just set it to failed with a message"). The split (restartable
    vs unrestartable+reason) is the pure `backends/restart.plan_restart`. Two surfaces, both through the
    rule-#1 cost gate (`confirm_launch(plan.items)`, one summary): a project-wide **Restart interrupted
    takes** action in the **Queue** tab header (ignoring view filters, like Cancel pending) and a per-take **Restart
    take** entry in the takes-grid context menu (`takes_view._build_context_menu`, no `exec()`, bubbles
    `restart_requested` up). Cancelling the gate fails nothing; only the unrestartable takes are failed,
    and only after a confirmed (or no-spend) restart.
    **`Take.interrupted` flag (2026-06-18):** "cancelled" / "failed" conflated user intent with
    crash damage — takes the user *deliberately* cancelled, and genuine render *failures*, vs takes
    a crash / ComfyUI-or-app *death* cut short: orphan recovery's CANCEL (a queued take never
    submitted) **and its FAIL** (an in-flight render lost to the restart — `generating`→FAILED when
    ComfyUI is unreachable, card from PR #52), the 3-strike `abandon_local` (its still-PENDING
    siblings AND, via the stamped `QueueAbandoned`, the crash victim itself that exhausted the
    retries - card #71), **and a take whose
    in-flight render was lost to a GPU TDR that crash recovery recovered from on the final restart
    (card #68 — re-raised FAILED, stamped `CRASH_INTERRUPTED_ATTR`, read in `jobs.py`'s else
    branch)**. The
    `interrupted: bool` field on `Take` records the difference: the **crash/death paths set it
    True** (both recovery CANCEL and FAIL, abandon_local and its queue-abandon crash victim,
    recovered-crash FAIL), every **manual
    cancel AND every genuine render failure set it False** — set explicitly at each terminal site,
    so a take crash-cancelled
    → restarted → user-cancelled (or genuinely failed) doesn't keep a stale True; `_restart_in_place`
    clears it too, and marking a take unrestartable-FAILED clears it (drops it from the set so it
    isn't retried forever). The **bulk** "Restart interrupted takes" re-runs every take with
    `interrupted and status in (CANCELLED, FAILED)` — so a lost in-flight render is restarted
    alongside the cancelled queue (`plan_restart` accepts both statuses;
    `main_window._interrupted_take_count` gates the action's enabled state). The **per-take**
    "Restart take" restarts *any* cancelled take (an explicit user override) **and a
    crash-interrupted FAILED take** (card #64) — so a single lost in-flight render can be
    surgically re-run from its own context menu, not only via the bulk action; both the menu gate
    (`takes_view._build_context_menu`) and the handler (`main_window._restart_takes_by_ids`) use the
    `cancelled or (failed and interrupted)` predicate, so a deliberately-FAILED (non-interrupted)
    take is still not offered. Additive/
    back-compat: `_take_from_dict` **backfills** a legacy take's flag from its `error` against
    `_INTERRUPTED_REASON_MARKERS` (the exact orphan-recovery/abandon phrases, NOT a bare "restart"
    substring — so a "cannot restart: …" unrestartable mark is not misread) so a pre-existing
    crashed batch is recognised on load. Smoke-tested in `smoke_phase2` (`test_restart_plan`,
    `test_restart_take`, `test_restart_from_snapshot`, `test_interrupted_flag`).
18. **The 2026-06-18 native stack-overflow crash is ROOT-CAUSED: a runaway
    `QWidgetPrivate::paintSiblingsRecursive` (a targeted fix is still pending).** The genuine
    crash was the **overnight run at 03:34 on 2026-06-18** (pid 42392), logged as `Windows fatal
    exception: stack overflow` in `animgen_faults.log`. It is a **native (C/C++) overflow on the
    GUI thread**, not Python: faulthandler dumped only shallow Python frames ("Current thread" =
    the main thread in `app.exec()`; the `_ws_progress_listener` recv chain is a *different*
    thread, printed first only by enumeration order, NOT the faulting frame). Now confirmed three
    ways: **(1)** WER **Event 1000** names faulting module **`Qt6Widgets.dll` 6.11.1.0**, exception
    **`0xC00000FD` (STATUS_STACK_OVERFLOW)**; **(2)** a full **minidump survived** (see below);
    **(3)** cracking that minidump with pure-Python `minidump` + `pefile` (no PDBs needed —
    `scripts/_dump_analyze.py` scans the faulting thread's stack, `scripts/_sym_nearest.py` maps
    hot RVAs to the nearest export) shows the faulting GUI thread (tid `0x9ef8`, matching
    faulthandler's "Current thread") with a **~1968 KiB stack — essentially the full ~2 MB
    main-thread limit, exhausted** — filled by **6913 copies of the same return address
    `Qt6Widgets.dll+0x62a15`** at a **288-byte stride** (6913 x 288 = ~1.99 MB = the whole stack),
    interleaved with Qt6Gui paint frames. `+0x62a15` sits +0x2e5 inside the nearest export
    **`QWidgetPrivate::paintSiblingsRecursive`** (the backingstore sibling-paint walk). So the
    crash is **unbounded / re-entrant recursion in Qt's sibling-paint path on the GUI thread** —
    and crucially it is **NOT bounded by AnimGen's widget count** (Biker has only 31 ShotCards;
    the takes grid and the Queue are delegate-painted `QListView`/`QTableView` with no per-item
    child widgets), so it is a true runaway cycle, not a "too many children" linear walk.
    **Trigger / what's RULED OUT (deeper hunt, 2026-06-18):** the WS thread is exonerated as the
    *faulting* frame (different thread). The earlier "minimized window" suspicion was WRONG — at
    the genuine 03:34 crash every heartbeat reads `minimized=False visible=True`, 1 shot tab, 183
    pending, RSS flat 342mb, dying 2s after a normal heartbeat (the `minimized=True` state was the
    *unrelated* 12:27 GPU event). The recursion is a **flat self-recursion** of
    `paintSiblingsRecursive` (no `drawWidget` frame between levels — confirmed by the per-module
    slot counts) and it skips hidden / non-intersecting siblings, so the dump demands **~6900
    VISIBLE, OVERLAPPING children under ONE parent**. The obvious churners were tested and
    **rejected** as that parent: the **Queue table** cell widgets (`QProgressBar`/`QPushButton`
    rebuilt each `refresh()`) do NOT leak under a real `app.exec()` loop — a manual-drain probe
    (`scripts/_leak_probe.py`) only "leaks" because `processEvents()` skips `DeferredDelete`; under
    a real loop (`scripts/_repro_loop.py`) the viewport child count *plateaus* (bounded by active
    rows), and table cells don't overlap anyway; the **takes grids** + **shot cards** are
    delegate-painted `QListView`/`QStandardItemModel` (no per-item child widgets; `load()` does
    `model.clear()`); `reload()` `deleteLater()`s old cards; the log is a `QPlainTextEdit` (text).
    A live census of the REAL Biker window (`scripts/_widget_census.py`) stays bounded (~649
    widgets, max ~101 children under any one parent) and doesn't grow under churn. So the
    accumulator is some **rare, conditional** path the offscreen runs don't reach (matching the
    "~1 in 14" rarity), NOT the hot per-take path. `ANIMGEN_NO_WS_PROGRESS=1` stays a bisection
    lever. **To NAME it on recurrence (instrumentation added 2026-06-18):** `applog._widget_census()`
    walks `QApplication.allWidgets()` on the GUI thread each **heartbeat** and logs
    `max_widgets=N(Class#objectName <ChildClassxN>)` — the direct analog of `max_pydepth` (GUI-thread
    only; `allWidgets()` isn't thread-safe so it's NOT in the watchdog daemon). On the next
    occurrence the heartbeat tail shows the offending container's child count climbing over the
    hours BEFORE the fatal repaint and names its class + dominant child type.
    **Minidump capture works, with one timing caveat:** `scripts/enable_crashdumps.py` writes the
    WER LocalDumps key `HKLM\...\LocalDumps\python.exe` (DumpFolder=`data/crashdumps`,
    DumpType=2 full, DumpCount=10). It was applied at 10:53 — *after* the 03:34 crash — so that
    dump went to WER's **default** folder `%LOCALAPPDATA%\CrashDumps\python.exe.42392.dmp`
    instead; it is preserved at `data/crashdumps/python.exe.42392.0334.stackoverflow.dmp`. Future
    crashes land in `data/crashdumps` directly. The rest of the instrumentation still stands: the
    **watchdog logs `max_pydepth`** (`applog._max_stack_depth`) — it stayed flat at 14, which is
    exactly what confirms the overflow is native, not Python. **Separately, the 12:27 'death' the
    same day was NOT this crash** — an nvlddmkm **GPU fault (System Event 153)** wedged ComfyUI
    mid-render and the python processes were terminated with **no** WER/minidump/faulthandler
    artifact (external kill, not a fault). Smoke: `smoke_phase2.test_ws_progress_diagnostics`.
