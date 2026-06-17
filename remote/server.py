"""Opt-in, localhost-only HTTP control server for driving the live AnimGen GUI.

Off by default; ``MainWindow`` starts it only when ``ANIMGEN_REMOTE`` is truthy. Binds
to 127.0.0.1 (``ANIMGEN_REMOTE_PORT``, default 8765; 0 = ephemeral). It runs on a daemon
thread and marshals every widget touch onto the GUI thread via ``GuiBridge`` — so it never
races the event loop and still works while a modal (the cost-confirm gate) is open.

Endpoints (JSON in/out unless noted):
    GET  /health                 -> liveness (no GUI round-trip)
    GET  /snapshot               -> {"widgets": [ {ref,class,name,text,rect,enabled,...} ]}
    GET  /screenshot[?ref=...]   -> image/png of the window (or one widget via QWidget.grab)
    POST /click  {ref|object_name|text}
    POST /type   {text, ref?|object_name?|text?}      (keystrokes; defaults to focus)
    POST /key    {key, ref?|object_name?}             (e.g. "enter", "tab", "escape")
    POST /set    {ref|object_name, value?|checked?}   (direct value set)
"""
from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, urlparse

from PySide6.QtWidgets import QApplication, QWidget

from remote import snapshot as snap
from remote.bridge import GuiBridge


class TargetNotFound(Exception):
    """No widget matched a click/type/key/set selector."""


def _require(window: QWidget, body: dict[str, Any]) -> QWidget:
    w = snap.resolve_target(
        window, body.get("ref"), body.get("object_name"), body.get("text"))
    if w is None:
        raise TargetNotFound(body)
    return w


def _focus_or_require(window: QWidget, body: dict[str, Any]) -> QWidget:
    if body.get("ref") or body.get("object_name") or body.get("text"):
        return _require(window, body)
    w = QApplication.focusWidget()
    if w is None:
        raise ValueError("no focused widget; provide 'ref', 'object_name', or 'text'")
    return w


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, handler, window: QWidget, bridge: GuiBridge):
        super().__init__(addr, handler)
        self.window = window
        self.bridge = bridge


class _Handler(BaseHTTPRequestHandler):
    server_version = "AnimGenRemote/1.0"

    # ---- plumbing ----
    def log_message(self, format, *args) -> None:  # noqa: A002,D401 - silence access log
        pass

    @property
    def _window(self) -> QWidget:
        return self.server.window  # type: ignore[attr-defined]

    def _call(self, fn: Callable[[], Any]) -> Any:
        return self.server.bridge.call(fn)  # type: ignore[attr-defined]

    def _read_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValueError(f"invalid JSON body: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError("JSON body must be an object")
        return data

    def _send_json(self, obj: Any, code: int = 200) -> None:
        payload = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _send_png(self, data: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _dispatch(self, handler: Callable[[], Any]) -> None:
        try:
            handler()
        except TargetNotFound as exc:
            self._send_json({"error": "target not found", "query": exc.args[0]}, 404)
        except ValueError as exc:
            self._send_json({"error": str(exc)}, 400)
        except TimeoutError as exc:
            self._send_json({"error": str(exc)}, 504)
        except Exception as exc:  # noqa: BLE001 - report, don't kill the server thread
            self._send_json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    # ---- routes ----
    def do_GET(self) -> None:  # noqa: N802 - http.server override
        route = urlparse(self.path)
        if route.path in ("/health", "/"):
            self._dispatch(lambda: self._send_json({"ok": True, "app": "AnimGen"}))
        elif route.path == "/snapshot":
            self._dispatch(self._route_snapshot)
        elif route.path == "/screenshot":
            query = parse_qs(route.query)
            ref = query.get("ref", [None])[0]
            self._dispatch(lambda: self._route_screenshot(ref))
        else:
            self._send_json({"error": f"unknown route {route.path}"}, 404)

    def do_POST(self) -> None:  # noqa: N802 - http.server override
        route = urlparse(self.path)
        try:
            body = self._read_body()
        except ValueError as exc:
            self._send_json({"error": str(exc)}, 400)
            return
        routes = {
            "/click": self._route_click,
            "/type": self._route_type,
            "/key": self._route_key,
            "/set": self._route_set,
        }
        fn = routes.get(route.path)
        if fn is None:
            self._send_json({"error": f"unknown route {route.path}"}, 404)
            return
        self._dispatch(lambda: fn(body))

    def _route_snapshot(self) -> None:
        widgets = self._call(lambda: snap.build_snapshot(self._window))
        self._send_json({"widgets": widgets})

    def _route_screenshot(self, ref: Optional[str]) -> None:
        def grab() -> bytes:
            target = self._window
            if ref:
                w = snap.resolve_target(self._window, ref=ref, object_name=ref)
                if w is None:
                    raise TargetNotFound({"ref": ref})
                target = w
            return snap.grab_png(target)

        self._send_png(self._call(grab))

    def _route_click(self, body: dict[str, Any]) -> None:
        def act() -> dict[str, Any]:
            w = _require(self._window, body)
            snap.do_click(w)
            return {"ok": True, "target": w.objectName() or type(w).__name__}

        self._send_json(self._call(act))

    def _route_type(self, body: dict[str, Any]) -> None:
        text = body.get("text")
        if not isinstance(text, str):
            raise ValueError("'text' (string) is required")

        def act() -> dict[str, Any]:
            w = _focus_or_require(self._window, body)
            snap.do_type(w, text)
            return {"ok": True, "target": w.objectName() or type(w).__name__}

        self._send_json(self._call(act))

    def _route_key(self, body: dict[str, Any]) -> None:
        key = body.get("key")
        if not isinstance(key, str):
            raise ValueError("'key' (string) is required")

        def act() -> dict[str, Any]:
            w = _focus_or_require(self._window, body)
            snap.do_key(w, key)
            return {"ok": True, "target": w.objectName() or type(w).__name__}

        self._send_json(self._call(act))

    def _route_set(self, body: dict[str, Any]) -> None:
        def act() -> dict[str, Any]:
            w = _require(self._window, body)
            result = snap.do_set(w, value=body.get("value"), checked=body.get("checked"))
            return {"ok": True, "target": w.objectName() or type(w).__name__, **result}

        self._send_json(self._call(act))


class RemoteControlServer:
    """Lifecycle wrapper: ``start()`` on the GUI thread, ``stop()`` on close."""

    def __init__(self, window: QWidget):
        self._window = window
        self._bridge = GuiBridge()  # constructed on (and thus lives on) the GUI thread
        self._httpd: Optional[_Server] = None
        self._thread: Optional[threading.Thread] = None
        self.port: Optional[int] = None

    def start(self) -> int:
        port = int(os.environ.get("ANIMGEN_REMOTE_PORT", "8765"))
        self._httpd = _Server(("127.0.0.1", port), _Handler, self._window, self._bridge)
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="animgen-remote", daemon=True)
        self._thread.start()
        return self.port

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
