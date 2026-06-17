# AnimGen

A native desktop app (PySide6) for turning keyposes into game-ready 2D animations.
Organize work into **projects**; each holds **shots** (start frame + optional end frame
+ framing + prompt + model + settings). Fire **generations** on hosted (Replicate) or
local (ComfyUI) backends behind a **cost-confirm gate**, **triage** the resulting
**takes** in a folder view (star / delete-to-bin / filter), and **export** selected
takes as frame sets with a settings record.

Every take stores an **immutable `settings_snapshot`**, so it stays linked to the exact
settings that produced it even if the shot is later edited.

> Originally built for the *Fighter* sprite project; AnimGen runs standalone and
> references that project (for keypose assets + the shipped-move seed manifest) and a
> ComfyUI install (for the local backend) as **external, overridable** locations.

## Setup

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt   # Windows
# .venv/bin/python -m pip install -r requirements.txt         # *nix
```

Hosted generation reads `REPLICATE_TOKEN` from a `.env` in the repo root (gitignored)
or the environment. Local generation needs ComfyUI on `127.0.0.1:8188`, started with
**`--disable-dynamic-vram --disable-async-offload`**. Easiest is the **Launch ComfyUI**
button in the app's **ComfyUI Status** tab (starts it detached with the flags, logs to
`data/comfyui_server.log`, and reports when it's ready). Equivalent from a terminal:

```bash
.venv/Scripts/python.exe scripts/launch_comfyui.py    # or: scripts\launch_comfyui.bat
```

Why the flags: ComfyUI has two independent engines that stream weights over PCIe
mid-render — dynamic VRAM (aimdo) and async weight offloading (default-on on Nvidia/AMD,
*not* covered by `--disable-dynamic-vram`). On the 12GB card either can stall a 14B render
past Windows' 2s GPU watchdog (TDR), which resets the driver and kills the server mid-job.
AnimGen refuses to launch a local job against a server still using either path (checked via
`/system_stats`); bypass with `ANIMGEN_ALLOW_DYNAMIC_VRAM=1`. Background:
`../Fighter/research/comfyui-gpu-watchdog-crash-and-aimdo.md`.

External locations default to siblings of the repo and can be overridden:

| Env var | Default | Used for |
|---|---|---|
| `ANIMGEN_FIGHTER_ROOT` | `../Fighter` | keypose assets, the shipped-move manifest |
| `ANIMGEN_COMFY_DIR` | `../comfyui` | local ComfyUI backend (input dir, output dir) |

## Run

```bash
.venv/Scripts/python.exe app.py
```

On launch AnimGen reopens your last project, else the seeded starter, else an empty
untitled project. Build the starter project from the source manifest (each move becomes
a shot with its approved take starred):

```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe scripts/seed_configs.py   # writes data/Fighter.animproj
```

## Concepts

- **Project** — a `.animproj` file plus a sidecar `<name>.assets/` folder (keyframe
  assets, takes, thumbnails, bin). **New / Open / Save / Save As** live in the **File**
  menu; the title bar shows the project name and a `*` for unsaved edits. Authoring edits
  buffer until you Save; a finished take is written to disk immediately so a render is
  never lost.
- **Shot** — one animation row: start/end keyframe (from the project's assets), a canvas
  **aspect ratio** (offered per model; the field flags red if invalid for the model), and
  per-keyframe drag/scale placement on that canvas, plus prompt + negative, model, settings.
  Double-click a row (or **+ New Shot**) to open the shot in its own editor tab.
- **Asset** — a keyframe image kept flat in `<name>.assets/`. The **Assets** tab lists
  them; drag images in (or Import) to copy them into the project. Shots reference assets
  for their start/end keyframes; the 1254px contract keypose is framed at generation time
  (no baked files on disk).
- **Take** — one generated `.mp4`: immutable `settings_snapshot`, status
  (`pending → generating → done/failed/cancelled`), star, soft-delete flag.
- **Model library** — `model_library.json`, a read-only roster (hosted + local) with
  cost/duration metadata. Open it from the **Model Library** tab, whose **Fetch live
  schemas** button pulls every Replicate model's per-parameter input schema and caches it
  (`data/schema_cache.json`) for the shot editor to reuse.
- **Cost-confirm gate** — every launch shows model + estimated spend + params and
  requires explicit confirmation. Default button is Cancel.
- **Cancel pending** — the toolbar button cancels every queued generation that hasn't
  started yet (they become `cancelled`); the in-progress one keeps running — stop that
  with the ComfyUI monitor's **Stop working**. The button enables only when something is
  queued.
- **ComfyUI Monitor** — the **ComfyUI Status** tab is a live view: status + version,
  RAM/VRAM use, what it's working on (queue), launch settings, and installed models.
  Controls: **Launch ComfyUI** (when down), **Stop working** (interrupt the current
  render + clear the queue, server stays up), and **Shut down** (stop the server process
  — terminates the one AnimGen launched, else kills whatever holds port 8188).
- **Bin** — delete moves project-owned files to the project's `<name>.assets/.bin/`
  (recoverable); files that live outside the project (e.g. a seeded take in the source
  project) are only flagged, never moved.
- **Export** — `<name>_<timestamp>/` folder of extracted PNG frames + `settings.txt`.
  Per take, per row (obeys the row's favorite/all filter), the whole view, or a
  multi-selection.

## Layout

| Path | Role |
|---|---|
| `app.py` | entry point |
| `paths.py` / `library.py` | path config / model-library loader |
| `model_library.json` | the model roster (authored) |
| `workflows/` | bundled ComfyUI templates for the local backend |
| `store/` | file-based **Project** document (`project.py`): shots / takes / jobs + dataclasses |
| `backends/` | replicate_client, comfy_client, job queue |
| `pipeline/` | framing, frame extraction, export, bin/restore |
| `ui/` | main window, shot cards, takes view, shot tab (editor), assets view, placement canvas + keyframe picker, cost gate, model-library + ComfyUI-monitor tabs |
| `scripts/seed_configs.py` | build the starter `Fighter.animproj` from the source manifest |
| `scripts/smoke_phase*.py` | headless smoke tests |
| `data/` | runtime (gitignored): `*.animproj` + each project's `<name>.assets/`, plus `exports/`, `_scratch/`, `app_state.json` |

## Tests

Headless smoke suites (no spend, no GPU, no real renderer):

```bash
for n in 1 2 3 4 5 6; do \
  QT_QPA_PLATFORM=offscreen PYTHONIOENCODING=utf-8 \
    .venv/Scripts/python.exe scripts/smoke_phase$n.py; done
```

A real hosted/local generation is gated by the cost dialog and not exercised by the
smoke tests (it spends money / GPU time).
