"""Phase 7 smoke test: the remote-control server (remote/).

Covers the pure GUI helpers (snapshot / resolve / action primitives) and a full marshalled
round-trip: a worker thread drives the live HTTP server (/health, /snapshot, /click,
/screenshot) while the main thread runs the Qt event loop, proving the bridge delivers
each call onto the GUI thread (the click actually fires there).

Run headless with the animgen venv:
    QT_QPA_PLATFORM=offscreen PYTHONIOENCODING=utf-8 \
        animgen/.venv/Scripts/python.exe animgen/scripts/smoke_phase7.py
"""
from __future__ import annotations

import json
import os
import sys
import threading
import urllib.request
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # animgen/

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication, QCheckBox, QLineEdit, QPushButton, QVBoxLayout, QWidget,
)

from remote import snapshot as snap  # noqa: E402

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _build_form() -> tuple[QWidget, QPushButton, QLineEdit, QCheckBox]:
    w = QWidget()
    w.setObjectName("root")
    lay = QVBoxLayout(w)
    btn = QPushButton("Generate")
    btn.setObjectName("genBtn")
    edit = QLineEdit()
    edit.setObjectName("nameEdit")
    edit.setPlaceholderText("name")
    chk = QCheckBox("Starred only")  # intentionally unnamed -> exercises Class:ordinal ref
    for child in (btn, edit, chk):
        lay.addWidget(child)
    return w, btn, edit, chk


def test_snapshot_and_resolve() -> None:
    app = _app()
    w, btn, edit, chk = _build_form()
    w.show()
    app.processEvents()

    widgets = snap.build_snapshot(w)
    by_ref = {d["ref"]: d for d in widgets}
    assert "genBtn" in by_ref and "nameEdit" in by_ref, sorted(by_ref)
    gen = by_ref["genBtn"]
    assert gen["class"] == "QPushButton" and gen["text"] == "Generate", gen
    assert gen["rect"][2] > 0 and gen["rect"][3] > 0, gen["rect"]

    assert snap.resolve_target(w, object_name="genBtn") is btn
    assert snap.resolve_target(w, text="Generate") is btn
    assert snap.resolve_target(w, ref="nameEdit") is edit
    chk_desc = next(d for d in widgets if d["class"] == "QCheckBox")
    assert snap.resolve_target(w, ref=chk_desc["ref"]) is chk  # Class:ordinal path
    assert snap.resolve_target(w, text="no such widget") is None
    print("snapshot/resolve OK")


def test_actions() -> None:
    app = _app()
    w, btn, edit, chk = _build_form()
    w.show()
    app.processEvents()

    clicks: list[int] = []
    btn.clicked.connect(lambda: clicks.append(1))
    snap.do_click(btn)
    assert clicks == [1], clicks

    snap.do_set(edit, value="hello")
    assert edit.text() == "hello"
    snap.do_type(edit, " world")
    assert "world" in edit.text(), edit.text()
    snap.do_set(chk, checked=True)
    assert chk.isChecked()

    try:
        snap.do_key(edit, "definitely-not-a-key")
        raise AssertionError("unknown key must raise")
    except ValueError:
        pass

    png = snap.grab_png(w)
    assert png[:8] == _PNG_MAGIC and len(png) > 100, len(png)
    print("actions OK")


def test_server_roundtrip() -> None:
    app = _app()
    win, btn, edit, chk = _build_form()
    win.show()
    app.processEvents()

    from remote.server import RemoteControlServer

    os.environ["ANIMGEN_REMOTE_PORT"] = "0"  # ephemeral port
    server = RemoteControlServer(win)
    port = server.start()
    base = f"http://127.0.0.1:{port}"

    clicks: list[int] = []
    btn.clicked.connect(lambda: clicks.append(1))
    results: dict[str, object] = {}

    def get(path: str) -> bytes:
        with urllib.request.urlopen(base + path, timeout=10) as resp:
            return resp.read()

    def post(path: str, body: dict) -> bytes:
        req = urllib.request.Request(
            base + path, data=json.dumps(body).encode("utf-8"), method="POST",
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.read()

    def worker() -> None:
        try:
            results["health"] = json.loads(get("/health"))
            results["snapshot"] = json.loads(get("/snapshot"))
            results["click"] = json.loads(post("/click", {"text": "Generate"}))
            results["png"] = get("/screenshot")
        except Exception as exc:  # noqa: BLE001 - surfaced as an assertion below
            results["error"] = repr(exc)
        finally:
            results["done"] = True

    threading.Thread(target=worker, daemon=True).start()

    poll = QTimer()
    poll.timeout.connect(lambda: results.get("done") and app.quit())
    poll.start(20)
    QTimer.singleShot(15000, app.quit)  # absolute safety net so the test can't hang
    app.exec()

    server.stop()
    assert results.get("error") is None, results.get("error")
    assert results["health"]["ok"] is True, results["health"]  # type: ignore[index]
    refs = {d["ref"] for d in results["snapshot"]["widgets"]}  # type: ignore[index]
    assert "genBtn" in refs, refs
    assert results["click"]["ok"] is True, results["click"]  # type: ignore[index]
    assert clicks == [1], "the click must have fired on the GUI thread via the bridge"
    assert results["png"][:8] == _PNG_MAGIC, "screenshot must be PNG"  # type: ignore[index]
    print("server round-trip OK")


if __name__ == "__main__":
    test_snapshot_and_resolve()
    test_actions()
    test_server_roundtrip()
    print("PHASE 7 SMOKE: PASS")
