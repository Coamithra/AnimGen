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
- **Trello (build board):** https://trello.com/b/7SycR6UZ  ("Animation Generator Tool")
- Origin/source project: *Fighter* (`../Fighter`), referenced at runtime — see "External wiring".

## Status (2026-06-16)

Built in 6 phases, **all 6 headless smoke suites pass** (`scripts/smoke_phase1-6.py`).
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
for n in 1 2 3 4 5 6; do QT_QPA_PLATFORM=offscreen PYTHONIOENCODING=utf-8 \
  .venv/Scripts/python.exe scripts/smoke_phase$n.py; done

# (re)build the starter project from the shipped-move manifest (idempotent;
# delete data/Fighter.animproj + data/Fighter.assets first for a clean rebuild)
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe scripts/seed_configs.py
```

Setup if `.venv` is missing: `python -m venv .venv` then
`.venv/Scripts/python.exe -m pip install -r requirements.txt`.

## Architecture map

| Path | Role |
|---|---|
| `app.py` | entry point (adds repo root to `sys.path`; opens the last/seeded/new Project, shows MainWindow) |
| `paths.py` | all paths + external-location config (see below); `DEFAULT_PROJECT`, `SCRATCH_DIR`, `APP_STATE` |
| `library.py` / `model_library.json` | model-roster loader (+ `aspect_ratios(model_id)`) + the hand-authored roster (per-model `aspect_ratios`; local `comfy_nodes.size_node`) |
| `store/project.py` `store/models.py` | file-based **Project** document (shots / takes / jobs) + dataclasses (`Shot`/`Take`/`Job`). Hybrid persistence: shot edits buffer (`dirty`, saved on `save()`); takes write through to `<assets>/takes.json`. Keyframe **assets** (`list_assets`/`import_asset`/`remove_asset` — image files flat in `.assets/`) + a load-time migration that flattens old `keyposes/<hash>/` baked files. RLock-guarded; atomic JSON writes |
| `backends/replicate_client.py` | hosted generation (refactor of Fighter's `run_replicate.py`) |
| `backends/comfy_client.py` | local ComfyUI generation (node-role mapping); also server lifecycle/status/preflight: `launch_server` (tracks the Popen in `_server_proc`), `stop_work` (interrupt+clear queue), `stop_server` (terminate ours, else kill by port via `_pid_on_port`/`_kill_pid`), `server_status`, `monitor_snapshot`, `list_models`, `build_launch_command`, `preflight`, `dynamic_vram_enabled` |
| `backends/jobs.py` | `JobManager` on QThreadPool; hosted parallel, local serialized; status signals; `cancel_pending`/`pending_count`; `set_project` to switch the active project. Worker threads call `project.update_take` (write-through) |
| `pipeline/framing.py` | `normalize_keypose` (contract framer) + `canvas_size(aspect, local=)` (hosted: longest side 1254; local: ~410k-px budget snapped to /16) + `render_keyposes(shot, dir)` (keys each keyframe sprite and places it `{scale,cx,cy}` on the aspect canvas at **generation time**) |
| `pipeline/extract.py` | frame extraction + thumbnails (PyAV) |
| `pipeline/export.py` | `export_takes` → `<name>_<timestamp>/` frames + `settings.txt` |
| `pipeline/takes_io.py` | bin / restore (only files under the project's `.assets/`; external refs left in place) |
| `ui/main_window.py` | shot cards + global filters + Generate/Export + **Cancel pending** (in the Shots-tab control strip); **File** menu project lifecycle (**New/Open/Save/Save As** + **New Shot**) with dirty-marker title + save-prompt; **closable** tabbed central widget (Shots / Assets / Model Library / ComfyUI Status; reopen from **View**). Double-click a shot row (or **+ New Shot**) opens a shot tab; shot tabs tracked in `shot_tabs` |
| `ui/comfy_monitor_window.py` | the **ComfyUI Status** tab: status/version, RAM+VRAM, queue, launch settings, installed models + **Launch ComfyUI**/Stop working/Shut down controls; `start_monitoring`/`stop_monitoring` poll only while the tab is visible (off-thread `_MonitorPoller` + `_AsyncCall` for stop/shutdown, same closed-port-timeout reason) |
| `ui/shot_card.py` `ui/takes_view.py` | shot row (double-click opens its shot tab; Generate / Export) + inline takes folder grid |
| `ui/shot_tab.py` | the **shot tab**: full editor + the shot's takes grid + Save/Generate/Export. Per-model **Aspect** dropdown (turns red + blocks Generate if invalid for the model); per-keyframe placement: left-click a keyframe to frame it, double-click to pick |
| `ui/placement_widget.py` | the framing canvas: drag a keyed sprite to position + a Size slider to scale it on the magenta aspect canvas; placement stored normalized `{scale,cx,cy}` |
| `ui/asset_picker.py` | the visual keyframe picker dialog (thumbnail grid + Import) |
| `ui/assets_view.py` | the **Assets** tab: drag-drop / Import keyframe images into `.assets/`; thumbnail grid + delete |
| `ui/cost_confirm.py` | the launch gate |
| `ui/model_library_window.py` | the **Model Library** tab: read-only model roster |
| `scripts/` | `seed_configs.py` (writes `Fighter.animproj`, imports keyframes as assets) + `smoke_phase*.py` + `launch_comfyui.py`/`.bat` (local backend, `--disable-dynamic-vram`) |
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
  render wide/tall). Each keyframe sprite is keyed + drag/scale-placed on that canvas at
  generation time. No baked keypose files / no `keyposes/<hash>/` folders.
- **Hybrid persistence:** authoring edits (add/rename/delete shots, prompts, framing)
  buffer in memory and set `dirty` (title shows `*`, prompt before discarding); a
  **completed Take auto-persists immediately** to `takes.json`. The split (shots in the
  `.animproj`, takes in `takes.json`) is what lets a finished render persist without
  flushing buffered shot edits.
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

## Hard-won rules / gotchas

1. **Cost-confirm gate before EVERY launch** (hosted or local). The dialog defaults to
   Cancel. Don't bypass it. Mirrors the source project's "ask before every generation".
2. **Additive — copy in, never move external originals.** Importing a keyframe asset
   COPIES it into `.assets/` (deliberate; the original is left untouched). Delete-to-bin
   only moves files under the project's `.assets/`; a take pointing at an external file
   (e.g. a seeded `../Fighter/out/` gif) is flagged deleted but left in place. Never
   relocate/delete anything outside the project.
3. **Each take stores an immutable `settings_snapshot`** — frozen at launch. This is
   the whole point (the source project had no per-take metadata). Don't mutate it.
4. **Smoke tests run headless** with `QT_QPA_PLATFORM=offscreen`; never call a modal's
   `.exec()` in a test (it blocks). Tests override `paths.SCRATCH_DIR` to a tempdir so
   untitled-project scratch stays out of `data/`. `build_summary` / pure functions are
   split out for exactly this reason.
5. **`model_library.json` is authored, not generated.** Replicate IDs/fields were
   verified via live schema fetch; per-param schemas are fetched live in the editor.
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
