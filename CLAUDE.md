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

## Architecture map

| Path | Role |
|---|---|
| `app.py` | entry point (adds repo root to `sys.path`; opens the last/seeded/new Project, shows MainWindow) |
| `paths.py` | all paths + external-location config (see below); `DEFAULT_PROJECT`, `SCRATCH_DIR`, `APP_STATE` |
| `library.py` / `model_library.json` | model-roster loader (+ `aspect_ratios(model_id)`, `sync_model_capabilities` — writes the two refresh-derived capability flags back into the roster, atomic+lock-guarded) + the roster (authored IDs/costs/notes/`aspect_ratios`/`supports_end_frame`, plus auto-synced `supports_negative_prompt`/`supports_camera_fixed`; local `comfy_nodes.size_node`) |
| `store/project.py` `store/models.py` | file-based **Project** document (shots / takes / jobs) + dataclasses (`Shot`/`Take`/`Job`). Hybrid persistence: shot edits buffer (`dirty`, saved on `save()`); takes write through to `<assets>/takes.json`. Keyframe **assets** (`list_assets`/`import_asset`/`remove_asset` — image files flat in `.assets/`) + a load-time migration that flattens old `keyposes/<hash>/` baked files. RLock-guarded; atomic JSON writes |
| `backends/replicate_client.py` | hosted generation (refactor of Fighter's `run_replicate.py`). `get_input_schema` **inlines enum `$ref`s** (`_resolve_enums`/`_follow_enum`/`_deref`): Replicate stores a property's allowed values as a `$ref`/`allOf`/`anyOf`/`oneOf` into `components.schemas`, not inline, so the resolver pulls each referenced `enum` (+ `type`) onto the property before returning — without this the editor never sees live options and silently falls back to the authored lists. `run_prediction`/`generate` take an `on_submit(pred_id)` callback (mirrors comfy's) that fires right after the create POST so the take records `backend_job_id` mid-render and becomes cancellable; `cancel_prediction(pred_id)` POSTs `/predictions/{id}/cancel` (best-effort, stops spend) |
| `backends/comfy_client.py` | local ComfyUI generation (node-role mapping); also server lifecycle/status/preflight: `launch_server` (tracks the Popen in `_server_proc`), `stop_work` (interrupt+clear queue), `stop_server` (terminate ours, else kill by port via `_pid_on_port`/`_kill_pid`), `server_status`, `monitor_snapshot`, `list_models`, `build_launch_command`, `preflight`, `dynamic_vram_enabled`; plus crash-recovery lifecycle: `wait_until_responsive` (poll `server_status` until the server answers) and `restart_server` (stop -> `launch_server` -> wait, used after a mid-render crash). During a render `submit()` opens a **best-effort progress WebSocket** (`/ws`) on a daemon thread and feeds per-step fractions to the UI; `progress_fraction()` (pure, headless-testable) maps `progress`/`progress_state` `value`/`max` → a 0..1 fraction |
| `backends/batch.py` | overnight **batch render** pure helpers (no Qt; dependency-injected, headless-testable): `plan_batch` (eligibility filter — unknown model / invalid aspect / no start frame skipped — + the `confirm_launch` item list, **N per eligible shot**), `BatchRun` (in-memory tracker; done when every take is terminal — done/failed/cancelled, which also covers 3-strike abandon), `build_batch_report` (morning summary), `sleep_command` (OS suspend argv), `POWER_NONE`/`POWER_STOP_COMFY`/`POWER_SLEEP`. The Qt side (queueing, the single cost gate, drain reaction, power action) lives in `main_window.start_batch`/`_finalize_batch`/`_perform_power_action`; the dialog is `ui/batch_dialog.py` |
| `backends/crash_recovery.py` | `run_with_crash_recovery` — wraps one local render with crash detection + auto-restart + 3-strike retry (pure / dependency-injected so it's headless-testable): a failure with ComfyUI **down** is a crash (restart the server, retry the same take in place), a failure with it **up** is a genuine workflow error (propagates, fails only that take); after `MAX_ATTEMPTS` crashes raises `QueueAbandoned` so the caller pauses the local queue. `format_elapsed` (pure) for the "failed in XmYs" note |
| `backends/jobs.py` | `JobManager` on QThreadPool; hosted parallel, local serialized; status signals; `cancel_pending`/`pending_count`; **`cancel_shot_takes(shot_id)`** (cancel just one shot's queued takes) + **`request_stop(take_id)`** (stop an in-flight GENERATING render — comfy `stop_work` / replicate `cancel_prediction`, best-effort; flags the take in `_stopping` so the worker's unwinding backend error records CANCELLED, not FAILED; **`is_stop_requested(take_id)`** lets the hosted runner's `on_submit` self-cancel a prediction whose create-POST returned only after the stop was requested — closing the window where `request_stop` skipped the cancel because `backend_job_id` wasn't recorded yet) + **`abandon_local(reason)`** (pause the local queue after repeated crashes — clears the local pool + cancels still-pending *comfyui* takes, hosted untouched, emits **`queue_abandoned`**); `set_project` to switch the active project. Worker threads call `project.update_take` (write-through). Emits **`progress_pct(take_id, fraction, label)`** (ephemeral, UI-only — never persisted) alongside the free-text `progress` signal; the runner callback is widened to `progress(line)` for milestones / `progress(frac=.., label=..)` for the step fraction |
| `pipeline/framing.py` | `normalize_keypose` (contract framer) + `canvas_size(aspect, local=)` (hosted: longest side 1254; local: ~410k-px budget snapped to /16) + `render_keyposes(shot, dir)` (keys each keyframe sprite and places it `{scale,cx,cy}` on the aspect canvas at **generation time**). `keyed_sprite(crop_to_content=True)` crops to the foreground bbox; placement/generation pass `crop_to_content=False` so the **full original frame** is keyed transparent and `scale` is relative to the **original image height**, not the cutout (two to-scale source frames at the same scale render at the same size) |
| `pipeline/extract.py` | frame extraction + thumbnails (PyAV) |
| `pipeline/export.py` | `export_takes` → `<name>_<timestamp>/` frames + `settings.txt` |
| `pipeline/takes_io.py` | bin / restore (only files under the project's `.assets/`; external refs left in place) |
| `ui/main_window.py` | shot cards + global filters + Generate/Export + **Cancel pending** + a right-aligned **Full set** total-price label (`_refresh_total_price`: sums `estimate_cost` over *all* shots, ignoring the view filters; unknown-rate shots tallied as `(+N unknown)`) (in the Shots-tab control strip); **File** menu project lifecycle (**New/Open/Save/Save As** + **New Shot**) with dirty-marker title (`*` when the project is untitled, has buffered edits, or any open shot tab has uncommitted edits — `_has_unsaved_changes`) + save-prompt; a **Settings** menu with a checkable **Update Replicate model data on startup** (persisted via `store/app_settings.py`; when on, `_maybe_refresh_schemas_on_startup` kicks off the Model Library tab's off-thread schema fetch at launch); **closable** tabbed central widget (Shots / Assets / Model Library / ComfyUI Status; reopen from **View**). Double-click a shot row (or **+ New Shot**) opens a shot tab; shot tabs tracked in `shot_tabs` |
| `ui/comfy_monitor_window.py` | the **ComfyUI Status** tab: status/version, RAM+VRAM, queue, launch settings, installed models + **Launch ComfyUI**/Stop working/Shut down controls; `start_monitoring`/`stop_monitoring` poll only while the tab is visible (off-thread `_MonitorPoller` + `_AsyncCall` for stop/shutdown, same closed-port-timeout reason) |
| `ui/shot_card.py` `ui/takes_view.py` | shot row (double-click opens its shot tab; Generate / Export; **right-click context menu** — Edit / Generate / Duplicate / Delete, built in `_build_context_menu` so it's headless-testable without `exec()`) + inline takes folder grid |
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
| `scripts/` | `seed_configs.py` (writes `Fighter.animproj`, imports keyframes as assets) + `smoke_phase*.py` + `remote_cli.py` (stdlib client for the `remote/` control server) + `launch_comfyui.py`/`.bat` (local backend, `--disable-dynamic-vram`) |
| `data/` | runtime (gitignored): `*.animproj` project files (default `Fighter.animproj`) + their sidecar `<name>.assets/` (flat keyframe images + `takes/`, `thumbs/`, `.bin/`); plus `exports/`, `_scratch/` (untitled-project assets), `app_state.json` (last opened) |
| `workflows/` | bundled ComfyUI templates for the local backend |

## Project model (file-based, 2026-06-16)

- A project is a `Foo.animproj` JSON file (`{format, version, name, shots:[...]}`,
  authoring data only) + a sidecar `Foo.assets/` folder beside it holding `takes.json`
  (take metadata), the flat keyframe **asset** images, and managed media (`takes/`,
  `thumbs/`, `.bin/`).
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
  flushing buffered shot edits. Note shot-tab editor edits set the tab's own dirty flag
  *before* they're committed to the buffer, so an open-but-unsaved shot tab shows `*` on
  its tab and contributes to the project title `*` even while `project.dirty` is still False.
- **Paths:** managed media + assets serialize **relative to `assets_dir`**; external
  references (seeded `../Fighter/out/*.gif`/`.mp4` takes) stay **absolute**. Untitled
  projects keep assets in `data/_scratch/<id>/` until the first **Save As**, which
  relocates them next to the chosen file.
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
   pure, headless-testable `format_generation_settings(take, shot=None)`.
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
10. **Local backend MUST run with `--disable-dynamic-vram`.** ComfyUI's default
    dynamic-VRAM (aimdo) engine stalls a 14B render past Windows' 2s GPU watchdog (TDR)
    on the 12GB card → driver reset → server dies mid-job, no traceback (Fighter, a night
    of crashes 2026-06-14; see `../Fighter/research/comfyui-gpu-watchdog-crash-and-aimdo.md`).
    AnimGen doesn't start ComfyUI, so it can't pass the flag — instead `comfy_client.preflight()`
    reads the server's launch `argv` from `/system_stats` and **refuses to submit a local
    job** if dynamic VRAM is enabled (mirrors ComfyUI's own `enables_dynamic_vram()` gate).
    Start ComfyUI with the flag baked in via the **Launch ComfyUI** button in the **ComfyUI Status** tab (detached,
    logs to `data/comfyui_server.log`) or `scripts/launch_comfyui.py`/`.bat` — all three share
    `comfy_client.build_launch_command()`. Escape hatch: `ANIMGEN_ALLOW_DYNAMIC_VRAM=1`.
    Note: probing a *down* localhost port costs a full socket timeout on this machine (SYNs
    to closed ports are dropped, not refused), so the **ComfyUI Status** tab polls on a
    daemon thread (`_MonitorPoller`, started only while that tab is visible) and never
    blocks the GUI thread on `server_status()`.
11. **Local-render progress comes over ComfyUI's WebSocket, best-effort.** ComfyUI exposes
    true per-step progress only on `/ws` (`progress` / `progress_state` messages with
    `value`/`max`) — there's no HTTP progress endpoint. `comfy_client.submit()` opens
    `ws://…/ws?clientId=…` on a daemon thread and translates each message into a 0..1
    fraction via `progress_fraction()` (pure → unit-tested in `smoke_phase2`), surfaced as a
    determinate **% bar** in the Queue tab (`ui/queue_view.py`). It is **non-fatal**: any WS
    failure is swallowed and the `/history` poll still drives the render to completion —
    and `/history` (not the last WS message) stays the authoritative *done* signal, because
    a documented `progress_state` tail keeps arriving ~20–30s after completion. Requires
    `websocket-client` (in `requirements.txt`). **Hosted (Replicate) exposes no native
    %** — progress lives only as free text in `logs` — so the Queue tab shows an
    indeterminate *busy* bar labelled with the elapsed-time line `run_prediction` already
    emits, rather than a percentage.
12. **Local renders auto-recover from a ComfyUI crash; 3 strikes pauses the queue.** A 14B
    render can still occasionally trip the 2s GPU watchdog (TDR) even with
    `--disable-dynamic-vram`, killing the ComfyUI *process* mid-render. The comfyui runner is
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
    rest" automatic — the other queued takes just wait behind it; nothing is re-enqueued. After
    `MAX_ATTEMPTS` (3) crashes on one take it raises `QueueAbandoned` -> `jobs.abandon_local()`
    cancels the remaining **local** pending takes (hosted untouched) and `queue_abandoned`
    surfaces a "queue paused" warning, so a legitimately-broken GPU can't trigger a restart
    loop. The retry/abandon notes ("failed in XmYs, retrying (attempt n/3)") flow through the
    normal `progress(line)` path, so they show on the take in the Queue tab and the main log.
    Each retry re-runs `preflight()`, so the dynamic-VRAM gate is never weakened. (Distinct from
    `backends/recovery.py`, which reconciles takes orphaned by an *app* restart on load.)
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
    is ambiguous (a few exist: `mainTabs`, `modelFilter`, `starredFilter`, `logDock`).
14. **Overnight batch render is one cost-confirm for the whole run, then unattended.**
    **Generate batch…** (Shots-tab control strip) queues every eligible shot × N takes after a
    **single** `confirm_launch(plan.items)` — this is how the per-launch cost gate (rule 1) is
    honored for an unattended run: the one dialog itemizes all N×shots and the full total, so no
    take fires without confirmation. Both backends (hosted parallel, local serialized as usual);
    each take reruns the random-seed roll in `_queue_take` so N takes vary. The render side is
    already unattended-safe (crash recovery + 3-strike abandon, write-through takes). A `BatchRun`
    (in `main_window`, in-memory, **not persisted** — a mid-batch app restart falls back to ordinary
    orphan recovery) tracks the take-ids; `_on_status_changed` marks each terminal take and, once all
    drained, `_finalize_batch` writes a report to `data/exports/overnight_<ts>.txt` and runs the chosen
    **when-finished** power action: stop ComfyUI (frees the GPU) and optionally sleep the PC
    (`batch.sleep_command()`), both best-effort on a daemon thread. Pure logic (`plan_batch`,
    `BatchRun`, `build_batch_report`, `sleep_command`) is in `backends/batch.py`, smoke-tested in
    `smoke_phase2.test_batch`.
