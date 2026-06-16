# AnimGen

A native desktop app (PySide6) for turning keyposes into game-ready 2D animations.
Author **generation configs** (start frame + optional end frame + framing + prompt +
model + settings), fire **generations** on hosted (Replicate) or local (ComfyUI)
backends behind a **cost-confirm gate**, **triage** results in a folder view
(star / delete-to-bin / filter), and **export** selected takes as frame sets with a
settings record.

Every result stores an **immutable `settings_snapshot`**, so a take stays linked to
the exact settings that produced it even if the config is later edited.

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
**`--disable-dynamic-vram`**. Easiest is the **Launch ComfyUI** toolbar button in the
app (starts it detached with the flag, logs to `data/comfyui_server.log`, and reports
when it's ready). Equivalent from a terminal:

```bash
.venv/Scripts/python.exe scripts/launch_comfyui.py    # or: scripts\launch_comfyui.bat
```

Why the flag: ComfyUI's default dynamic-VRAM (aimdo) engine streams weights mid-render
and, on the 12GB card, stalls a 14B render past Windows' 2s GPU watchdog (TDR), which
resets the driver and kills the server mid-job. AnimGen refuses to launch a local job
against a server that has dynamic VRAM enabled (checked via `/system_stats`); bypass
with `ANIMGEN_ALLOW_DYNAMIC_VRAM=1`. Background:
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

Seed starting configs from the source project's shipped-move manifest (each move
becomes a config with its approved take as a starred result):

```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe scripts/seed_configs.py
```

## Concepts

- **Config** — one animation row: start/end keypose, framing (crop + canvas + ground
  line + placement), prompt + negative, model, and model settings.
- **Result** — one generated take: immutable `settings_snapshot`, status
  (`pending → generating → done/failed/cancelled`), star, soft-delete flag.
- **Model library** — `model_library.json`, a read-only roster (hosted + local) with
  cost/duration metadata; Replicate per-parameter schemas are fetched live in the
  editor. Open it from the **Model Library** toolbar button.
- **Cost-confirm gate** — every launch shows model + estimated spend + params and
  requires explicit confirmation. Default button is Cancel.
- **Cancel pending** — the toolbar button cancels every queued generation that hasn't
  started yet (they become `cancelled`); the in-progress one keeps running — stop that
  with the ComfyUI monitor's **Stop working**. The button enables only when something is
  queued.
- **ComfyUI Monitor** — the **ComfyUI Status** toolbar button opens a live window:
  status + version, RAM/VRAM use, what it's working on (queue), launch settings, and
  installed models. Controls: **Launch** (when down), **Stop working** (interrupt the
  current render + clear the queue, server stays up), and **Shut down** (stop the server
  process — terminates the one AnimGen launched, else kills whatever holds port 8188).
- **Bin** — delete moves tool-owned files to `data/bin/` (recoverable); files that
  live outside the repo (e.g. a seeded take in the source project) are only flagged,
  never moved.
- **Export** — `<name>_<timestamp>/` folder of extracted PNG frames + `settings.txt`.
  Per result, per row (obeys the row's favorite/all filter), the whole view, or a
  multi-selection.

## Layout

| Path | Role |
|---|---|
| `app.py` | entry point |
| `paths.py` / `library.py` | path config / model-library loader |
| `model_library.json` | the model roster (authored) |
| `workflows/` | bundled ComfyUI templates for the local backend |
| `store/` | SQLite store (configs / results / jobs) + dataclasses |
| `backends/` | replicate_client, comfy_client, job queue |
| `pipeline/` | framing, frame extraction, export, bin/restore |
| `ui/` | main window, config cards, results view, config editor, crop widget, cost gate, model library window |
| `scripts/seed_configs.py` | seed configs from the source manifest |
| `scripts/smoke_phase*.py` | headless smoke tests |
| `data/` | runtime (gitignored): `animgen.db`, `results/`, `bin/`, `exports/`, `thumbs/`, `keyposes/` |

## Tests

Headless smoke suites (no spend, no GPU, no real renderer):

```bash
for n in 1 2 3 4 5 6; do \
  QT_QPA_PLATFORM=offscreen PYTHONIOENCODING=utf-8 \
    .venv/Scripts/python.exe scripts/smoke_phase$n.py; done
```

A real hosted/local generation is gated by the cost dialog and not exercised by the
smoke tests (it spends money / GPU time).
