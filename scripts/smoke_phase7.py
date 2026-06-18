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
import time
import urllib.error
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


def test_tab_widget() -> None:
    """A named QTabWidget (like the app's `mainTabs`) reports its tab titles and switches
    via /set value=<title> — the inner unnamed QTabBar is not the only tab handle."""
    from PySide6.QtWidgets import QTabWidget

    app = _app()
    root = QWidget()
    lay = QVBoxLayout(root)
    tw = QTabWidget()
    tw.setObjectName("mainTabs")
    tw.addTab(QWidget(), "Shots")
    tw.addTab(QWidget(), "Assets")
    lay.addWidget(tw)
    root.show()
    app.processEvents()

    desc = next(d for d in snap.build_snapshot(root) if d["ref"] == "mainTabs")
    assert desc["tabs"] == ["Shots", "Assets"], desc
    assert desc["current"] == 0, desc
    res = snap.do_set(snap.resolve_target(root, object_name="mainTabs"), value="Assets")
    assert res == {"current": 1} and tw.currentIndex() == 1, res
    print("tab widget OK")


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


def test_monitor_poller_supersede() -> None:
    """start()/stop()/start() in quick succession must leave exactly ONE live poller thread
    probing the port; superseded threads exit and stop probing (card #62). Stubs
    comfy_client.monitor_snapshot with a slow probe so a supersede races a blocked worker."""
    _app()  # _MonitorPoller is a QObject; ensure an application exists
    from backends import comfy_client
    from ui.comfy_monitor_window import _MonitorPoller

    lock = threading.Lock()
    probes: list[int] = []  # idents of threads that called monitor_snapshot

    def fake_snapshot(timeout: float = 2.0):
        with lock:
            probes.append(threading.get_ident())
        time.sleep(0.05)  # simulate the blocking socket probe so a supersede can race it
        return {}

    orig = comfy_client.monitor_snapshot
    comfy_client.monitor_snapshot = fake_snapshot
    try:
        poller = _MonitorPoller(interval=0.02)
        poller.start()
        t1 = poller._thread
        poller.stop()
        poller.start()      # supersede while t1 may still be blocked in fake_snapshot
        t2 = poller._thread
        poller.start()      # double-start without stop -> also superseded
        t3 = poller._thread
        assert t1 is not None and t2 is not None and t3 is not None

        deadline = time.time() + 3.0
        while time.time() < deadline and (t1.is_alive() or t2.is_alive()):
            time.sleep(0.02)
        assert not t1.is_alive(), "first poller thread did not exit when superseded"
        assert not t2.is_alive(), "second poller thread did not exit when superseded"
        assert t3.is_alive(), "the latest poller thread should still be running"

        with lock:
            probes.clear()
        time.sleep(0.25)  # several poll intervals - only the survivor should probe
        with lock:
            seen = set(probes)
        assert seen == {t3.ident}, f"expected only the live poller to probe, got {seen}"

        poller.stop()
        deadline = time.time() + 3.0
        while time.time() < deadline and t3.is_alive():
            time.sleep(0.02)
        assert not t3.is_alive(), "poller thread did not exit after stop()"
    finally:
        comfy_client.monitor_snapshot = orig
    print("monitor poller supersede OK")


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

    def post_status(path: str, raw_body: bytes) -> int:
        req = urllib.request.Request(
            base + path, data=raw_body, method="POST",
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status
        except urllib.error.HTTPError as exc:
            return exc.code

    def worker() -> None:
        try:
            results["health"] = json.loads(get("/health"))
            results["snapshot"] = json.loads(get("/snapshot"))
            results["click"] = json.loads(post("/click", {"text": "Generate"}))
            results["png"] = get("/screenshot")
            # negative paths: missing target -> 404, bad JSON -> 400, non-checkable -> 400
            results["miss"] = post_status("/click", b'{"ref": "no-such-widget"}')
            results["badjson"] = post_status("/click", b"{not json")
            results["noncheck"] = post_status("/set", b'{"ref": "genBtn", "checked": true}')
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
    assert results.get("done"), "round-trip never finished (timed out / deadlocked)"
    assert results.get("error") is None, results.get("error")
    assert results["health"]["ok"] is True, results["health"]  # type: ignore[index]
    refs = {d["ref"] for d in results["snapshot"]["widgets"]}  # type: ignore[index]
    assert "genBtn" in refs, refs
    assert results["click"]["ok"] is True, results["click"]  # type: ignore[index]
    assert clicks == [1], "the click must have fired on the GUI thread via the bridge"
    assert results["png"][:8] == _PNG_MAGIC, "screenshot must be PNG"  # type: ignore[index]
    assert results["miss"] == 404, results.get("miss")
    assert results["badjson"] == 400, results.get("badjson")
    assert results["noncheck"] == 400, results.get("noncheck")  # genBtn isn't checkable
    print("server round-trip OK (+ 404/400 negative paths)")


if __name__ == "__main__":
    test_snapshot_and_resolve()
    test_tab_widget()
    test_actions()
    test_monitor_poller_supersede()
    test_server_roundtrip()
    print("PHASE 7 SMOKE: PASS")
