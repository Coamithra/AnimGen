# AnimGen — pickup guide

Native **PySide6 desktop app** that turns keyposes into game-ready 2D animations:
author **generation configs** (start frame + optional end frame + framing + prompt +
model + settings), fire **generations** on hosted (Replicate) or local (ComfyUI)
backends behind a **cost-confirm gate**, **triage** results in a folder view
(star / delete-to-bin / filter), and **export** selected takes as frame sets + a
settings record. Extracted from the *Fighter* sprite project on 2026-06-13.

## Project links

- **GitHub:** https://github.com/Coamithra/AnimGen  (public, `main`)
- **Trello (build board):** https://trello.com/b/7SycR6UZ  ("Animation Generator Tool")
- Origin/source project: *Fighter* (`../Fighter`), referenced at runtime — see "External wiring".

## Status (2026-06-13)

Built in 6 phases, **all 6 headless smoke suites pass** (`scripts/smoke_phase1-6.py`).
DB seeds 31 shipped moves from Fighter's manifest. **Still pending (needs explicit
go-ahead — it spends money / GPU):** a live hosted take and a live local take. The
backends are verified offline only.

## Run / test / seed

```bash
# from the repo root
.venv/Scripts/python.exe app.py                      # launch the app (Windows)

# headless smoke tests (no spend, no GPU, no real renderer)
for n in 1 2 3 4 5 6; do QT_QPA_PLATFORM=offscreen PYTHONIOENCODING=utf-8 \
  .venv/Scripts/python.exe scripts/smoke_phase$n.py; done

# seed configs from the source project's shipped-move manifest (idempotent)
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe scripts/seed_configs.py
```

Setup if `.venv` is missing: `python -m venv .venv` then
`.venv/Scripts/python.exe -m pip install -r requirements.txt`.

## Architecture map

| Path | Role |
|---|---|
| `app.py` | entry point (adds repo root to `sys.path`, opens the store, shows MainWindow) |
| `paths.py` | all paths + external-location config (see below) |
| `library.py` / `model_library.json` | model-roster loader + the hand-authored roster |
| `store/db.py` `store/models.py` | SQLite store (configs / results / jobs) + dataclasses |
| `backends/replicate_client.py` | hosted generation (refactor of Fighter's `run_replicate.py`) |
| `backends/comfy_client.py` | local ComfyUI generation (wraps `run_workflow.py`; node-role mapping) |
| `backends/jobs.py` | `JobManager` on QThreadPool; hosted parallel, local serialized; status signals |
| `pipeline/framing.py` | keypose crop/normalize → 1254² magenta contract canvas |
| `pipeline/extract.py` | frame extraction + thumbnails (PyAV) |
| `pipeline/export.py` | `<name>_<timestamp>/` frames + `settings.txt` |
| `pipeline/results_io.py` | bin / restore |
| `ui/main_window.py` | config cards in a scroll area + global filters + Generate/Export wiring |
| `ui/config_card.py` `ui/results_view.py` | expandable row + inline results folder grid |
| `ui/config_editor.py` `ui/crop_widget.py` | create/edit config + the crop/framing tool |
| `ui/cost_confirm.py` | the launch gate |
| `ui/model_library_window.py` | read-only model roster window |
| `scripts/` | `seed_configs.py` + `smoke_phase*.py` |
| `data/` | runtime (gitignored): `animgen.db`, `results/`, `bin/`, `exports/`, `thumbs/`, `keyposes/` |
| `workflows/` | bundled ComfyUI templates for the local backend |

## External wiring (overridable env vars)

- `ANIMGEN_FIGHTER_ROOT` (default `../Fighter`) — keypose assets + the seed manifest.
- `ANIMGEN_COMFY_DIR` (default `../comfyui`) — local ComfyUI (input/output dirs).
- **`REPLICATE_TOKEN`** — read from the environment, then a repo-local `.env`, then
  the source project's `.env`. The token is **never committed** (`.env` is gitignored).

## Hard-won rules / gotchas

1. **Cost-confirm gate before EVERY launch** (hosted or local). The dialog defaults to
   Cancel. Don't bypass it. Mirrors the source project's "ask before every generation".
2. **Purely additive / never touch external assets.** Delete-to-bin only moves files
   under `data/`; a result that points at an external file (e.g. a seeded Fighter take
   in `../Fighter/out/`) is flagged deleted but the file is left in place.
3. **Each result stores an immutable `settings_snapshot`** — frozen at launch. This is
   the whole point (the source project had no per-take metadata). Don't mutate it.
4. **Smoke tests run headless** with `QT_QPA_PLATFORM=offscreen`; never call a modal's
   `.exec()` in a test (it blocks). `build_summary` / pure functions are split out for
   exactly this reason.
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
