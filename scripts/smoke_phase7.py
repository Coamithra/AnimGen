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
    QApplication, QCheckBox, QComboBox, QDoubleSpinBox, QLineEdit, QPushButton, QSpinBox,
    QVBoxLayout, QWidget,
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


def test_resolve_prefers_visible() -> None:
    """A text-targeted resolve must land on the *visible* duplicate, not an earlier hidden
    one (e.g. a same-text 'Generate' button on an inactive shot tab) — matching
    build_snapshot's visibility gate. A hidden match is only returned when none is visible."""
    app = _app()
    root = QWidget()
    root.setObjectName("root2")
    lay = QVBoxLayout(root)
    hidden_btn = QPushButton("Generate")  # built/parented first -> earlier in tree order
    visible_btn = QPushButton("Generate")
    lay.addWidget(hidden_btn)
    lay.addWidget(visible_btn)
    root.show()
    app.processEvents()
    hidden_btn.hide()
    app.processEvents()

    assert not hidden_btn.isVisible() and visible_btn.isVisible(), "setup: one hidden, one shown"
    # Without the fix, raw tree order returns the hidden button first (it diverged from the
    # snapshot, which already omits it) -> /click by text then 400s as 'not visible'.
    assert snap.resolve_target(root, text="Generate") is visible_btn, "exact: must prefer visible"
    assert snap.resolve_target(root, text="gen") is visible_btn, "substring: must prefer visible"

    visible_btn.hide()  # now no visible match exists -> a hidden one is an acceptable fallback
    app.processEvents()
    # hidden group is still searched; exact-first + tree order picks the earlier hidden_btn
    assert snap.resolve_target(root, text="Generate") is hidden_btn, "hidden fallback"
    print("resolve prefers visible OK")


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


def test_do_set_spinbox_and_combo() -> None:
    """L16: do_set drives QSpinBox/QDoubleSpinBox; a non-editable combo with an unknown value
    fails honestly (raises, not a silent ok:true); a numeric-cast failure raises."""
    app = _app()
    root = QWidget()
    lay = QVBoxLayout(root)
    spin = QSpinBox()
    spin.setRange(0, 100)
    dspin = QDoubleSpinBox()
    dspin.setRange(0.0, 10.0)
    dspin.setDecimals(2)
    combo = QComboBox()  # non-editable by default
    combo.addItems(["alpha", "beta"])
    ecombo = QComboBox()
    ecombo.setEditable(True)
    ecombo.addItems(["one", "two"])
    for wdg in (spin, dspin, combo, ecombo):
        lay.addWidget(wdg)
    root.show()
    app.processEvents()

    assert snap.do_set(spin, value="42") == {"value": 42} and spin.value() == 42
    res = snap.do_set(dspin, value="3.5")
    assert res == {"value": 3.5} and abs(dspin.value() - 3.5) < 1e-9, res

    try:
        snap.do_set(spin, value="not-a-number")
        raise AssertionError("non-numeric spinbox value must raise")
    except ValueError:
        pass

    # Non-editable combo: a known value selects it; an unknown value raises (no silent no-op).
    assert snap.do_set(combo, value="beta")["currentText"] == "beta" and combo.currentIndex() == 1
    before = combo.currentIndex()
    try:
        snap.do_set(combo, value="gamma")
        raise AssertionError("unknown value on a non-editable combo must raise")
    except ValueError:
        pass
    assert combo.currentIndex() == before, "a failed combo set must not change the selection"

    # Editable combo accepts a free-text value.
    assert snap.do_set(ecombo, value="three")["currentText"] == "three", ecombo.currentText()

    # Spinbox surfaces its value in a snapshot.
    desc = next(d for d in snap.build_snapshot(root) if d["class"] == "QSpinBox")
    assert desc["value"] == 42, desc
    print("do_set spinbox/combo OK")


def test_negative_ordinal_rejected() -> None:
    """L16: a negative Class:ordinal ref resolves to None (404), not a Python negative index."""
    app = _app()
    root = QWidget()
    lay = QVBoxLayout(root)
    b0 = QPushButton("zero")
    b1 = QPushButton("one")
    for b in (b0, b1):
        lay.addWidget(b)
    root.show()
    app.processEvents()

    assert snap.resolve_target(root, ref="QPushButton:0") is b0
    assert snap.resolve_target(root, ref="QPushButton:1") is b1
    assert snap.resolve_target(root, ref="QPushButton:-1") is None, "negative ordinal must 404"
    assert snap.resolve_target(root, ref="QPushButton:99") is None, "out-of-range must 404"
    print("negative ordinal rejected OK")


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

    def fake_snapshot(timeout: int = 2):
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

        # t1/t2 are confirmed dead, so only the survivor can probe from here. Poll until it
        # has probed at least once (robust to a slow/loaded scheduler), then assert it alone.
        with lock:
            probes.clear()
        deadline = time.time() + 3.0
        seen: set[int] = set()
        while time.time() < deadline and not seen:
            time.sleep(0.02)
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


def test_bridge_cancel_once() -> None:
    """L15: GuiBridge.call cancellation is atomic wrt execution. Two cases:
    (1) a call that never gets to run before the timeout must NOT execute afterward (no late
        side effect / double-action);
    (2) a call the GUI thread starts just as the caller times out must return its REAL result,
        not a spurious 504 — the timeout waits for it instead of racing a re-send."""
    app = _app()
    from remote.bridge import GuiBridge

    bridge = GuiBridge()  # lives on the GUI (main) thread
    ran: list[str] = []
    results: dict[str, Any] = {}

    # Case 1: the GUI event loop is NOT pumped until AFTER the worker's call has timed out, so
    # the posted event is still queued when the timeout fires. It must be cancelled and never
    # run once the loop finally turns.
    def worker_timeout() -> None:
        try:
            bridge.call(lambda: ran.append("late") or "value", timeout=0.1)
            results["case1"] = "returned"  # should not happen
        except TimeoutError:
            results["case1"] = "timeout"
        finally:
            results["case1_done"] = True

    threading.Thread(target=worker_timeout, daemon=True).start()
    time.sleep(0.3)  # let the worker post its event and time out while the loop is idle
    # Now pump the loop: the queued _CallEvent is delivered but must find itself cancelled.
    deadline = time.time() + 2.0
    while time.time() < deadline and not results.get("case1_done"):
        app.processEvents()
        time.sleep(0.01)
    app.processEvents()
    assert results.get("case1") == "timeout", results
    assert ran == [], f"a timed-out call must NOT execute afterward, got {ran}"

    # Case 2: a normal call under a live loop returns its result (claim-then-run path).
    results2: dict[str, Any] = {}

    def worker_ok() -> None:
        results2["val"] = bridge.call(lambda: "hello", timeout=5.0)
        results2["done"] = True

    threading.Thread(target=worker_ok, daemon=True).start()
    deadline = time.time() + 3.0
    while time.time() < deadline and not results2.get("done"):
        app.processEvents()
        time.sleep(0.01)
    assert results2.get("val") == "hello", results2

    # Case 3 (the timeout-boundary branch): the GUI thread CLAIMS and is still running a slow
    # fn when the caller's timeout expires. The caller must see the claim, wait for the real
    # result, and return it — not raise a spurious 504 while the action executes anyway.
    results3: dict[str, Any] = {}
    slow_ran: list[str] = []

    def slow_fn() -> str:
        slow_ran.append("ran")
        time.sleep(0.6)  # far longer than the caller's timeout below
        return "slow-result"

    def worker_boundary() -> None:
        try:
            results3["val"] = bridge.call(slow_fn, timeout=0.15)
        except TimeoutError:
            results3["val"] = "TIMEOUT"
        finally:
            results3["done"] = True

    threading.Thread(target=worker_boundary, daemon=True).start()
    # Pump immediately: the GUI thread claims + enters slow_fn while the 0.05s timeout expires
    # mid-run (processEvents blocks in slow_fn for 0.4s, so the interleave is deterministic).
    deadline = time.time() + 3.0
    while time.time() < deadline and not results3.get("done"):
        app.processEvents()
        time.sleep(0.01)
    assert slow_ran == ["ran"], slow_ran
    assert results3.get("val") == "slow-result", (
        f"caller must wait for the claimed call's real result, got {results3.get('val')!r}")
    print("bridge cancel once OK")


def test_dispatch_no_double_response() -> None:
    """L20: _dispatch must not re-send after a response-write failure. When a handler starts a
    response and then the socket write raises (BrokenPipe), the exception is a dead-socket
    write error — it must propagate, NOT trigger a second _send_json on the same socket. A
    handler error raised BEFORE any bytes are written still maps to one error response."""
    from remote.server import TargetNotFound, _Handler

    handler = _Handler.__new__(_Handler)  # bypass __init__ (no real socket needed)
    sent: list[tuple] = []

    def record_send(obj, code=200):
        # Mirror the real _send_json: mark response-started, then "write". Here the write
        # itself blows up on the SECOND call — but with the fix there is never a second call.
        handler._response_started = True
        sent.append((code, obj))

    handler._send_json = record_send  # type: ignore[assignment]

    # (1) Handler that writes a good response, then its write path dies mid-payload.
    handler._response_started = False
    sent.clear()

    def handler_writes_then_dies():
        handler._send_json({"ok": True})          # response begins here
        raise BrokenPipeError("client hung up mid-write")

    try:
        handler._dispatch(handler_writes_then_dies)
        raise AssertionError("a post-response write error must propagate, not be swallowed")
    except BrokenPipeError:
        pass
    assert len(sent) == 1, f"exactly ONE response must be sent, got {len(sent)}: {sent}"

    # (2) Handler error BEFORE any response -> one mapped error response (404 here).
    handler._response_started = False
    sent.clear()
    handler._dispatch(lambda: (_ for _ in ()).throw(TargetNotFound({"ref": "x"})))
    assert len(sent) == 1 and sent[0][0] == 404, sent
    print("dispatch no-double-response OK")


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
    edit.setFocus()  # L1: /type {text} must land here (focus), not on the "Generate" button
    app.processEvents()
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
            # L1: bare {text} types into the FOCUSED field (nameEdit), not the "Generate"
            # button that shares no text with the payload — proving text is no longer a selector.
            results["type_focus"] = json.loads(post("/type", {"text": "abc"}))
            # L1: {keys, ref} selects the widget explicitly and types keys into it.
            results["type_keys"] = json.loads(post("/type", {"keys": "XY", "ref": "nameEdit"}))
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
    # L1: the bare {text} typed into the focused nameEdit; {keys, ref} appended into the same
    # field. Neither routed to the button (clicks stayed at 1) and the field holds both.
    assert results["type_focus"]["target"] == "nameEdit", results["type_focus"]  # type: ignore[index]
    assert results["type_keys"]["target"] == "nameEdit", results["type_keys"]  # type: ignore[index]
    assert edit.text() == "abcXY", f"type must land in the field, got {edit.text()!r}"
    assert results["miss"] == 404, results.get("miss")
    assert results["badjson"] == 400, results.get("badjson")
    assert results["noncheck"] == 400, results.get("noncheck")  # genBtn isn't checkable
    print("server round-trip OK (+ L1 type-into-focus, 404/400 negative paths)")


if __name__ == "__main__":
    test_snapshot_and_resolve()
    test_resolve_prefers_visible()
    test_tab_widget()
    test_do_set_spinbox_and_combo()
    test_negative_ordinal_rejected()
    test_actions()
    test_bridge_cancel_once()
    test_dispatch_no_double_response()
    test_monitor_poller_supersede()
    test_server_roundtrip()
    print("PHASE 7 SMOKE: PASS")
