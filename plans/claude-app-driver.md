# Plan: Allow Claude to drive AnimGen (embedded control server)

Trello card "Allow Claude to drive" (6a3269b1). AnimGen is a native PySide6 desktop app,
so Claude can't test-drive it like a web app and keeps asking for full-PC control. This
adds an **opt-in, localhost-only HTTP control server inside the app** that exposes
web-like hooks (screenshot / widget snapshot / click / type / key / set). Claude drives
it with `curl` — scoped to the app, deterministic (ref/objectName/text targeting, not
pixels), and **no OS screen-capture or PC-control grant needed** (screenshots come from
Qt's `QWidget.grab()`).

## Design

New package `remote/` (off by default; mirrors the off-thread-poller discipline already
used by the ComfyUI Status tab):

- `remote/bridge.py` — `GuiBridge(QObject)`. The HTTP server runs on a daemon thread but
  every widget touch must happen on the GUI thread. `bridge.call(fn)` posts `fn` to the
  GUI thread via a custom `QEvent` (`QApplication.postEvent` is thread-safe), blocks the
  caller until it runs there, and returns the result / re-raises. Same-thread calls run
  inline (no deadlock). Modals run a nested event loop, so calls still work while the
  cost-confirm dialog is open — the gate is driven, never bypassed.
- `remote/snapshot.py` — pure, headless-testable: `build_snapshot(window)` → flat list of
  the relevant visible widgets (`{ref, class, name, text, rect[x,y,w,h] in window coords,
  visible, enabled}`), `resolve_target(window, ref/object_name/text)`, `grab_png(widget)`,
  and the action primitives (`do_click`, `do_type`, `do_key`, `do_set`). ref is a
  deterministic `Class:ordinal` (or the objectName when present) so it survives a re-walk.
- `remote/server.py` — `RemoteControlServer(window)` wrapping `ThreadingHTTPServer` bound
  to `127.0.0.1`. `start()` (port from `ANIMGEN_REMOTE_PORT`, default 8765, 0 = ephemeral)
  / `stop()`. Endpoints: `GET /health`, `GET /snapshot`, `GET /screenshot[?ref=]`,
  `POST /click`, `POST /type`, `POST /key`, `POST /set`. Each Qt op goes through the bridge.

Wiring in `ui/main_window.py`:
- `_maybe_start_remote()` (called at the end of `__init__`) starts the server only when
  `ANIMGEN_REMOTE` is truthy; logs the URL into the generation log.
- `closeEvent` stops it.
- Add `objectName`s to a curated set of otherwise-ambiguous controls (`mainTabs`,
  `modelFilter`, `starredFilter`); everything else is reachable by visible text.

Convenience: `scripts/remote_cli.py` — thin `curl`-equivalent (`health|snapshot|shot|
click|type|key|set`) so driving is one command and screenshots save to a file.

## Tests

`scripts/smoke_phase7.py` (headless, offscreen):
- `build_snapshot` surfaces a named button + line edit with correct text/rect.
- `resolve_target` by ref / objectName / text.
- action primitives: `do_click` fires a button's slot; `do_set` sets line-edit text; etc.
- `grab_png` returns non-empty PNG bytes.
- full marshalled round-trip: start the server, drive `/health` + `/snapshot` + `/click`
  from a worker thread while the main thread runs the event loop, assert the click landed.
Update the smoke-loop references (CLAUDE.md, CONTRIBUTING.md) from 1-6 to 1-7.

## Out of scope

- Naming every widget in the app (text/ref targeting already covers the rest).
- Auth / non-localhost exposure (dev-only aid; off by default, 127.0.0.1 only).
- Driving native OS dialogs (QFileDialog etc. are OS-native; use the project-lifecycle
  endpoints / pre-set paths instead — noted as a known limitation).

## Verification

Headless smoke 1-7 must pass. Manual: launch with `ANIMGEN_REMOTE=1`, drive via
`scripts/remote_cli.py` (snapshot, screenshot, click a tab) to confirm a real round-trip.
