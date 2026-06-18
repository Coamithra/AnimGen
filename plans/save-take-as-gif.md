# Plan: Save take as animated GIF (disk or clipboard) — card #70

## Context
A user viewing a take in the in-app player (`ui/take_player.py`) wants to export it as an
animated GIF — either to a file on disk or straight to the clipboard — primarily via
right-click on the video. This is a pure additive UI/export feature: no persistence
schema, no backend, no framing, no cost gate involved. Small blast radius.

## Findings (Phase 2)
- `ui/take_player.py` already has a clean, exec()-free `_build_context_menu()` (the
  "Show generation settings" entry) — the natural place to add menu items.
- `take_source(take)` (same file) returns the playable file path (real `.mp4`, else gif).
- `pipeline/extract.iter_frames(path)` yields **PIL `Image.Image`** frames via PyAV;
  `video_info(path)` returns `{"fps", "frames"}`. `Take.fps`/`Take.frame_count` are also stored.
- **Pillow** is in `requirements.txt` → `Image.save(save_all=True, append_images=…,
  duration=…, loop=0, disposal=2)` writes animated GIFs natively. No new dependency.
- No existing clipboard usage; `QFileDialog.getSaveFileName` is already used in
  `ui/main_window.py` — match that convention.
- Take-player tests live in `scripts/smoke_phase5.py` (`test_take_player_settings_panel`
  builds a `TakePlayerTab` headless and triggers context-menu actions without `.exec()`).

## Design

### Pure encoder — `pipeline/gif_export.py` (new, Qt-free, headless-testable)
```python
def encode_gif(frames: list[Image.Image], out_path, fps: float, *,
               loop: int = 0, max_side: int | None = None) -> Path
def take_to_gif(source: str, out_path, *, fps: float | None = None,
                max_side: int | None = None, max_frames: int = 1200) -> Path
```
- `take_to_gif` decodes `source` with `extract.iter_frames`, falls back to `video_info`
  fps (then a 12.0 default), caps at `max_frames` (mirrors the player's `_MAX_FRAMES`),
  and delegates to `encode_gif`.
- `encode_gif` converts each frame to a palette via Pillow (`disposal=2` so frames don't
  ghost), `duration = round(1000 / fps)` ms per frame, `loop=0` (infinite).
- Optional `max_side` downscales (clipboard copies stay light); `None` = full resolution.

### UI — `ui/take_player.py`
- Add to `_build_context_menu()`:
  - **"Save as GIF…"** → `_save_gif()`: `QFileDialog.getSaveFileName` (default name
    `<shot>_<take>.gif`), then `take_to_gif(source, path)`. Status feedback via the canvas
    label / a transient message.
  - **"Copy GIF to clipboard"** → `_copy_gif()`: encode to a persisted temp `.gif`, then
    set a file-URL `QMimeData` on the clipboard (decision below).
- Both entries are disabled / no-op when the take has no playable source.
- Encoding runs on a short-lived daemon thread (same pattern as `_FrameLoader`) so a
  longer clip can't freeze the GUI; the clipboard set marshals back to the GUI thread.

### Clipboard approach — FILE ONLY (user's call 2026-06-18)
`QMimeData.setUrls([QUrl.fromLocalFile(gif_path)])` → CF_HDROP on Windows, so pasting into
Discord / email / Explorer attaches the **animated** `.gif`. The temp file must outlive the
copy (paste happens later), so it's written `delete=False` to a stable per-take path under
the system temp dir (`tempfile.gettempdir()/animgen_gif_clip/<take>.gif`), reused on repeat
copies so they don't pile up. No bitmap/`image/gif` payloads — a file reference is tiny, so
Copy uses the same full-resolution encode as Save (no downscale needed).

## Tests (Phase 5)
- Extend `scripts/smoke_phase5.py`:
  - Pure `encode_gif` / `take_to_gif`: encode a few synthetic PIL frames, assert the output
    is a valid animated GIF (`Image.open(...).is_animated`, `n_frames`, `info["loop"]==0`).
  - `TakePlayerTab` context menu exposes "Save as GIF…" + "Copy GIF to clipboard"
    (built without `.exec()`), and `_save_gif` to a temp path produces a GIF (monkeypatch
    `QFileDialog.getSaveFileName` like the existing asset-import test does).
  - `_copy_gif` populates the clipboard `QMimeData` with a file URL pointing at a real
    on-disk `.gif` (offscreen clipboard works headless).

## Out of scope
- No toolbar/button surface (right-click only, per the card) unless requested.
- No per-export options dialog (fps/scale/loop pickers) unless requested — sensible
  one-click defaults (source fps, full res for Save / downscaled for Copy, infinite loop).
- No change to take persistence, snapshot, backends, or the cost gate.

## Verification
- Headless: all 7 smoke phases pass (phase 5 gains the new coverage).
- Manual (UI): launch the app, open a take, right-click → Save as GIF (open the file in a
  viewer to confirm it animates) and → Copy GIF, paste into an image editor and a chat app.
