"""Guarded Qt signal emit for daemon-thread workers (card #48).

A worker/daemon thread that emits a Qt Signal after its owning C++ QObject was deleted
(window/app/project teardown while the thread is still running) raises
``RuntimeError('Signal source has been deleted')``. When that emit happens on a thread
whose Python frame is invoked from C++ (a ``QRunnable.run`` override, a daemon thread's
callback), the uncaught exception aborts the whole process at the C++ layer
(std::terminate / an exit-time SIGSEGV) with no Python traceback.

``guarded_emit`` degrades that to a dropped signal: ``shiboken6.isValid`` gates the common
case and the ``try/except RuntimeError`` closes the race between the check and the emit.
A dropped signal only costs a UI refresh - the worker has already persisted its state
(write-through) before signalling.

This is the shared implementation behind ``jobs.GenerationJob._emit`` and
``model_library_window._ReplicateRefresher._emit``; the five other daemon-thread emit
sites (jobs.abandon_local, takes_view._StripLoader, take_player._FrameLoader /
_GifExporter, main_window._OrphanReconciler) route through it directly.
"""
from __future__ import annotations

import shiboken6


def guarded_emit(obj, signal_name: str, *args) -> None:
    """Emit ``obj.<signal_name>(*args)``, silently dropping it if ``obj``'s C++ half is gone.

    ``obj`` is the QObject that owns the signal (a ``_JobSignals`` instance, a worker
    ``QObject``, ...). Returns without raising whether the object is already invalid or is
    torn down in the window between the validity check and the emit.
    """
    try:
        if not shiboken6.isValid(obj):
            return
        getattr(obj, signal_name).emit(*args)
    except RuntimeError:
        pass
