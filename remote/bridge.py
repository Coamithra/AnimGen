"""Thread-safe GUI-call marshalling for the remote-control server.

The HTTP server runs on a daemon thread, but every Qt widget touch must happen on the
GUI thread. ``GuiBridge.call(fn)`` posts ``fn`` to the GUI thread via a custom ``QEvent``
(``QApplication.postEvent`` is documented thread-safe), blocks the calling thread until
``fn`` has run there, and returns its result (or re-raises its exception). A modal dialog
runs its own nested event loop, so posted calls are still delivered while the cost-confirm
gate is open — the gate is driven, never bypassed.
"""
from __future__ import annotations

import threading
from typing import Any, Callable

from PySide6.QtCore import QEvent, QObject, QThread
from PySide6.QtWidgets import QApplication


class _CallEvent(QEvent):
    """Carries a zero-arg callable to run on the GUI thread."""

    _TYPE = QEvent.Type(QEvent.registerEventType())

    def __init__(self, run: Callable[[], None]):
        super().__init__(_CallEvent._TYPE)
        self.run = run


class GuiBridge(QObject):
    """Lives on the GUI thread; runs callables posted from worker threads there."""

    def call(self, fn: Callable[[], Any], timeout: float = 15.0) -> Any:
        # Calling from the GUI thread itself would deadlock on the event below
        # (the loop that must deliver it is the very thread we'd block) — run inline.
        if QThread.currentThread() is self.thread():
            return fn()

        holder: dict[str, Any] = {}
        done = threading.Event()
        # `claimed` is decided ONCE, under `lock`, by whichever of the two racing threads
        # wins: the GUI thread claims it before running `fn`, the caller claims it on timeout.
        # This closes the L15 race where a 504-reported call could still execute afterward —
        # the loser sees the flag already set and skips (no late side effect / double-click).
        lock = threading.Lock()
        state = {"claimed": False}

        def run() -> None:
            with lock:
                if state["claimed"]:  # caller already timed out and gave up; skip it entirely
                    return
                state["claimed"] = True
            try:
                holder["result"] = fn()
            except BaseException as exc:  # noqa: BLE001 - relayed to the caller thread
                holder["error"] = exc
            finally:
                done.set()

        QApplication.postEvent(self, _CallEvent(run))
        if not done.wait(timeout):
            with lock:
                if not state["claimed"]:  # the GUI thread hasn't started fn — cancel it
                    state["claimed"] = True
                    raise TimeoutError(f"GUI call did not complete within {timeout:.1f}s")
            # The GUI thread claimed it just as we timed out; it's running/ran to completion.
            # Wait for it so we return its real result instead of a spurious 504.
            done.wait()
        if "error" in holder:
            raise holder["error"]
        return holder.get("result")

    def event(self, ev: QEvent) -> bool:  # noqa: N802 - Qt override
        if ev.type() == _CallEvent._TYPE:
            ev.run()  # type: ignore[attr-defined]
            return True
        return super().event(ev)
