# Plan: Store & surface generation settings with each take (card #33)

Card: https://trello.com/c/LGPuz0a2

## Context

A take already stores an immutable `settings_snapshot`, frozen at launch
(`ui/main_window.generate_shot`), and `pipeline/export.py` already writes that snapshot
verbatim into `settings.txt` (with a *separate*, clearly-labelled "current shot values —
may have changed" block). So the headline worry on the card — "the export txt is incorrect
because the shot was edited" — is largely already handled by the snapshot.

Two genuine gaps remain:

1. **The snapshot is incomplete.** It omits `canvas_w` / `canvas_h` and `crop`
   (aspect + start/end placement). Those drive framing and are editable post-generation, so
   the only record of the canvas/framing a take was rendered with is the *live* shot block,
   which may have drifted. → add them to the snapshot.
2. **There is no in-app way to see a take's generation settings.** The card asks for a
   settings button (bottom-right of the viewer, next to the frame timer) AND a right-click
   "Show generation settings" item on the video, either of which opens a docked panel to the
   right of the video showing that take's original generation settings.

## Design

### 1. Complete the snapshot (`ui/main_window.generate_shot`)
Add `canvas` and `crop` to the snapshot dict:
```python
snapshot = {
    ...existing...,
    "canvas": [shot.canvas_w, shot.canvas_h],
    "crop": shot.crop,
}
```
Only affects new takes; existing takes keep their current (smaller) snapshot. No migration
needed — `settings_snapshot` is a free-form dict and all readers use `.get`.

### 2. Pure formatter (`ui/take_player.py`, module-level)
`format_generation_settings(take, shot=None) -> str` — turns a take's `settings_snapshot`
into a human-readable, ordered plain-text block (model display name via `library.get_model`,
backend, prompt, negative, seed, canvas, then the `settings` dict sorted). Falls back
gracefully when the snapshot is sparse (older takes) or empty. Pure + headless-testable
(no Qt), mirroring the `build_summary` / `progress_fraction` split convention.

### 3. Docked settings panel in `TakePlayerTab` (`ui/take_player.py`)
- Wrap the existing canvas in a `QSplitter` (horizontal): canvas on the left, a settings
  panel (`QTextEdit`, read-only) on the right. Panel hidden by default.
- Add a **⚙ Settings** `QPushButton` to the controls row, right of `frame_label`. Clicking
  it toggles the panel (checkable button).
- Give the canvas a context menu (`setContextMenuPolicy(CustomContextMenu)` +
  `customContextMenuRequested`) with a single **Show generation settings** action that opens
  (un-hides) the panel — built in a `_build_context_menu()` returning a `QMenu` so it's
  testable without `exec()` (same pattern as `shot_card._build_context_menu`).
- Panel text is filled from `format_generation_settings` on first open (the take + its shot
  are already available via `self.project`).

### Reusable patterns leaned on
- Pure-function-split for headless testing (`progress_fraction`, `build_summary`).
- `_build_context_menu()` returning a `QMenu` (from `ui/shot_card.py`).
- `library.get_model` for the display name (as `queue_view._model_backend` does).

## Tests (`scripts/smoke_phase5.py`, alongside the existing export test)
- `format_generation_settings` renders a full snapshot (model name, prompt, seed, canvas,
  settings) and degrades cleanly on an empty/sparse snapshot.
- `generate_shot`'s snapshot now carries `canvas` + `crop` (extend an existing generate path
  test, or assert on a constructed snapshot).
- `TakePlayerTab` builds headless, the ⚙ button toggles the panel's visibility, and
  `_build_context_menu()` yields a "Show generation settings" action that reveals the panel —
  all without `.exec()`.

## Out of scope
- No change to how framing actually renders (still live-shot at generation time).
- No backfill/migration of canvas/crop onto pre-existing takes' snapshots.
- No edit/copy affordance in the panel — it's a read-only provenance view.
- `settings.txt` format unchanged beyond the snapshot now also carrying canvas/crop.
