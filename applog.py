"""Crash / shutdown / liveness diagnostics for AnimGen.

The goal: know EXACTLY when, why, and how the app stops. Everything lands in
data/animgen.log (rotating), plus native-crash dumps in data/animgen_faults.log.

WHAT GETS CAPTURED
  * startup context     - pid, python/Qt versions, argv, cwd, relevant env, project
  * heartbeat (GUI)      - every ANIMGEN_HEARTBEAT_S (default 10s), logged FROM the Qt
                          event loop, with rss/threads + project/job context. Proves the
                          UI is alive and pumping; its last timestamp bounds time-of-death.
  * watchdog (thread)    - every ANIMGEN_WATCHDOG_S (default 20s), logged from a plain
                          daemon thread that does NOT touch Qt. Survives a GUI freeze, so:
                          GUI beats stop but watchdog continues  => UI HANG (it dumps all
                          thread stacks); both stop               => process is gone.
  * window close         - with spontaneous() (you/OS closed it vs programmatic) + hide
  * OS session end       - logoff / shutdown / restart (commitDataRequest)
  * Python crashes       - uncaught exceptions on the main thread (sys.excepthook) AND on
                          worker threads (threading.excepthook), with full tracebacks
  * NATIVE crashes       - faulthandler dumps the stack on SIGSEGV/SIGABRT/SIGFPE/etc and
                          on a Windows access violation (a Qt/C++ segfault that never
                          raises a Python exception - the usual "it just vanished" cause)
  * Qt fatals            - qFatal/qCritical via the Qt message handler
  * OS signals           - SIGTERM/SIGINT/SIGBREAK (polite terminate)
  * exit                 - aboutToQuit, event-loop exit code, atexit

READING IT WHEN IT VANISHES (look at the tail of data/animgen.log)
  - ends with "window closing (spontaneous=True)" + clean trail -> a normal close
  - a traceback / "Qt FATAL" / a dump in animgen_faults.log        -> a real crash
  - GUI heartbeats stop, watchdog keeps ticking                    -> UI HANG (froze)
  - BOTH stop with no shutdown line and no fault dump              -> hard external kill
                          (Task Manager "End task", OOM, GPU-driver reset) - uncatchable
  - "OS SESSION ENDING"                                            -> Windows logged off/shut down
  - rss climbing across heartbeats just before it dies            -> likely OOM

Dependency-light: psutil is used if present (richer stats) else a ctypes/stdlib fallback.
"""
from __future__ import annotations

import atexit
import faulthandler
import logging
import os
import signal
import sys
import threading
import time
from logging.handlers import RotatingFileHandler

import paths

logger = logging.getLogger("animgen")
_configured = False
_fault_file = None              # kept open for faulthandler's lifetime
_last_gui_beat: float = 0.0     # monotonic ts of the last GUI heartbeat (0 = none yet)
_heartbeat_timer = None         # keep a ref so the QTimer isn't GC'd

HEARTBEAT_S = float(os.environ.get("ANIMGEN_HEARTBEAT_S", "10"))
WATCHDOG_S = float(os.environ.get("ANIMGEN_WATCHDOG_S", "20"))


# ---- process stats ------------------------------------------------------------
def _rss_mb() -> float | None:
    try:
        import psutil
        return psutil.Process().memory_info().rss / 1e6
    except Exception:  # noqa: BLE001
        pass
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            class _PMC(ctypes.Structure):
                _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                            ("PeakWorkingSetSize", ctypes.c_size_t),
                            ("WorkingSetSize", ctypes.c_size_t),
                            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                            ("PagefileUsage", ctypes.c_size_t),
                            ("PeakPagefileUsage", ctypes.c_size_t)]

            c = _PMC()
            c.cb = ctypes.sizeof(_PMC)
            h = ctypes.windll.kernel32.GetCurrentProcess()
            if ctypes.windll.psapi.GetProcessMemoryInfo(h, ctypes.byref(c), c.cb):
                return c.WorkingSetSize / 1e6
        except Exception:  # noqa: BLE001
            pass
    return None


def _os_threads() -> int | None:
    try:
        import psutil
        return psutil.Process().num_threads()
    except Exception:  # noqa: BLE001
        return None


def _proc_stats() -> str:
    rss = _rss_mb()
    osn = _os_threads()
    return (f"rss={rss:.0f}mb" if rss is not None else "rss=?") + \
           f" py_threads={threading.active_count()}" + \
           (f" os_threads={osn}" if osn is not None else "")


def _max_stack_depth() -> tuple[int, str]:
    """Deepest Python call stack across all live threads, as (frames, thread_name).

    Diagnostic for the 2026-06-18 native stack-overflow crash: a native (C/C++) overflow
    leaves SHALLOW Python frames, so if this stays flat across watchdog ticks right up to the
    death, the overflow is in Qt/C++, not Python recursion; a CLIMBING value would instead
    finger runaway Python recursion (and name the thread). Cheap - one sys._current_frames()
    walk per watchdog tick."""
    names = {t.ident: t.name for t in threading.enumerate()}
    best, who = 0, "?"
    for ident, frame in sys._current_frames().items():
        depth, f = 0, frame
        while f is not None:
            depth += 1
            f = f.f_back
        if depth > best:
            best, who = depth, names.get(ident, str(ident))
    return best, who


def _widget_census() -> str:
    """The parent widget holding the most child widgets, as a compact heartbeat field.

    Companion to _max_stack_depth for the rule-#18 paintSiblingsRecursive stack overflow:
    that crash needs ~thousands of visible sibling widgets under ONE parent, and the obvious
    churning views (Queue table, takes grids) were proven bounded - so the real accumulator is
    elsewhere and rare. A climbing count here NAMES the offending container (class + objectName)
    and its dominant child class BEFORE the fatal repaint, turning 'somewhere' into one line.
    MUST run on the GUI thread - it walks QApplication.allWidgets() - so it's called only from
    the heartbeat, never the watchdog daemon thread. Returns '' if there's no QApplication;
    swallows any error so it can never break the heartbeat."""
    try:
        from PySide6.QtWidgets import QApplication, QWidget
        app = QApplication.instance()
        if not isinstance(app, QApplication):
            return ""
        counts: dict[int, int] = {}
        parents: dict[int, QWidget] = {}
        for w in app.allWidgets():
            p = w.parentWidget()
            if p is not None:
                pid = id(p)
                counts[pid] = counts.get(pid, 0) + 1
                parents[pid] = p
        if not counts:
            return "max_widgets=0"
        from collections import Counter
        pid = max(counts, key=lambda k: counts[k])
        p = parents[pid]
        kids = Counter(type(c).__name__ for c in p.children() if c.isWidgetType())
        dom = kids.most_common(1)
        domstr = f" <{dom[0][0]}x{dom[0][1]}>" if dom else ""
        obj = p.objectName()
        name = type(p).__name__ + (f"#{obj}" if obj else "")
        return f"max_widgets={counts[pid]}({name}{domstr})"
    except Exception as e:  # noqa: BLE001
        return f"max_widgets_err={e!r}"


# ---- setup --------------------------------------------------------------------
def setup() -> logging.Logger:
    """Install file+stderr logging, faulthandler, uncaught-exception hooks (main + worker
    threads), OS signal handlers, and an atexit marker. Idempotent."""
    global _configured, _fault_file
    if _configured:
        return logger
    _configured = True

    paths.ensure_dirs()
    fmt = logging.Formatter("%(asctime)s %(levelname)-8s [%(threadName)s] %(message)s")
    fileh = RotatingFileHandler(paths.DATA_DIR / "animgen.log", maxBytes=5_000_000,
                                backupCount=5, encoding="utf-8")
    fileh.setFormatter(fmt)
    streamh = logging.StreamHandler()
    streamh.setFormatter(fmt)
    logger.setLevel(logging.INFO)
    logger.addHandler(fileh)
    logger.addHandler(streamh)

    # Native-crash dumps (segfault / abort / Windows access violation). These never raise a
    # Python exception, so without faulthandler a Qt/C++ crash leaves NO trace at all.
    try:
        _fault_file = open(paths.DATA_DIR / "animgen_faults.log", "a", encoding="utf-8")
        _fault_file.write(f"\n===== faulthandler armed pid={os.getpid()} =====\n")
        _fault_file.flush()
        faulthandler.enable(file=_fault_file, all_threads=True)
    except Exception as e:  # noqa: BLE001
        logger.warning("faulthandler unavailable: %r", e)

    try:
        from PySide6 import __version__ as pyside_ver
        from PySide6.QtCore import qVersion
        qt = f"PySide6 {pyside_ver} / Qt {qVersion()}"
    except Exception:  # noqa: BLE001
        qt = "Qt unknown"
    env = {k: os.environ.get(k) for k in
           ("ANIMGEN_REMOTE", "ANIMGEN_REMOTE_PORT", "ANIMGEN_FIGHTER_ROOT",
            "ANIMGEN_COMFY_DIR", "ANIMGEN_ALLOW_DYNAMIC_VRAM") if os.environ.get(k)}
    logger.info("=== STARTUP  pid=%s  python=%s  %s  platform=%s ===",
                os.getpid(), sys.version.split()[0], qt, sys.platform)
    logger.info("context  cwd=%s  argv=%s  env=%s  heartbeat=%ss watchdog=%ss",
                os.getcwd(), sys.argv, env or "{}", HEARTBEAT_S, WATCHDOG_S)

    # Uncaught Python exceptions on the MAIN thread.
    prev_hook = sys.excepthook

    def _excepthook(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            logger.warning("KeyboardInterrupt")
        else:
            logger.critical("UNCAUGHT EXCEPTION (crash):", exc_info=(exc_type, exc, tb))
        prev_hook(exc_type, exc, tb)

    sys.excepthook = _excepthook

    # Uncaught exceptions on WORKER threads (JobManager render workers, pollers, ...).
    def _thread_hook(args):
        if issubclass(args.exc_type, SystemExit):
            return
        logger.critical("UNCAUGHT THREAD EXCEPTION in %s:",
                        getattr(args.thread, "name", "?"),
                        exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    threading.excepthook = _thread_hook

    def _on_signal(signum, _frame):
        try:
            name = signal.Signals(signum).name
        except ValueError:
            name = str(signum)
        logger.warning("RECEIVED SIGNAL %s (%s) - terminating", signum, name)
        raise SystemExit(128 + signum)

    for signame in ("SIGTERM", "SIGINT", "SIGBREAK"):
        sig = getattr(signal, signame, None)
        if sig is not None:
            try:
                signal.signal(sig, _on_signal)
            except (ValueError, OSError):
                pass

    atexit.register(lambda: logger.info("=== ATEXIT  process exiting  pid=%s ===", os.getpid()))
    return logger


# ---- Qt-side hooks (call after a QApplication exists) -------------------------
def install_qt_message_handler() -> None:
    """Route Qt's own diagnostics (qWarning/qCritical/qFatal) into the log."""
    from PySide6.QtCore import QtMsgType, qInstallMessageHandler

    level = {QtMsgType.QtDebugMsg: logging.DEBUG, QtMsgType.QtInfoMsg: logging.INFO,
             QtMsgType.QtWarningMsg: logging.WARNING, QtMsgType.QtCriticalMsg: logging.ERROR,
             QtMsgType.QtFatalMsg: logging.CRITICAL}

    def _handler(msg_type, _context, message):
        tag = "Qt FATAL" if msg_type == QtMsgType.QtFatalMsg else "Qt"
        logger.log(level.get(msg_type, logging.INFO), "%s: %s", tag, message)

    qInstallMessageHandler(_handler)


def install_session_logging(app) -> None:
    """Log when the OS ends the session (logoff / shutdown / restart) - this is how you
    tell 'Windows closed it' from 'I closed it'."""
    try:
        app.commitDataRequest.connect(
            lambda _sm: logger.warning("OS SESSION ENDING (logoff/shutdown/restart)"))
        app.saveStateRequest.connect(
            lambda _sm: logger.info("OS session saveStateRequest"))
    except Exception as e:  # noqa: BLE001
        logger.info("session-end logging unavailable: %r", e)


def start_heartbeat(parent, context_fn=None) -> None:
    """GUI-thread heartbeat via a QTimer. Logs liveness + stats from inside the event loop,
    so if these lines stop the UI loop has stalled/died. `context_fn` (optional) returns a
    short app-state string (project/jobs)."""
    from PySide6.QtCore import QTimer
    global _heartbeat_timer

    def beat():
        global _last_gui_beat
        _last_gui_beat = time.monotonic()
        extra = ""
        if context_fn is not None:
            try:
                extra = " " + context_fn()
            except Exception as e:  # noqa: BLE001
                extra = f" ctx_err={e!r}"
        logger.info("heartbeat(gui)  %s%s %s", _proc_stats(), extra, _widget_census())

    t = QTimer(parent)
    t.setInterval(int(HEARTBEAT_S * 1000))
    t.timeout.connect(beat)
    t.start()
    _heartbeat_timer = t
    beat()  # immediate first beat


def start_watchdog() -> None:
    """Background daemon thread that ticks independently of Qt. Detects a frozen GUI (its
    heartbeat goes stale) and dumps every thread's stack so you can see WHERE it's stuck."""
    def loop():
        hung = False
        while True:
            time.sleep(WATCHDOG_S)
            age = (time.monotonic() - _last_gui_beat) if _last_gui_beat else None
            depth, who = _max_stack_depth()
            logger.info("watchdog  %s  gui_beat_age=%s  max_pydepth=%d(%s)", _proc_stats(),
                        f"{age:.0f}s" if age is not None else "n/a", depth, who)
            if age is not None and age > HEARTBEAT_S * 3:
                if not hung:
                    logger.warning("GUI UNRESPONSIVE for %.0fs (possible hang/freeze) - "
                                   "dumping all thread stacks to animgen_faults.log", age)
                    try:
                        if _fault_file is not None:
                            faulthandler.dump_traceback(file=_fault_file, all_threads=True)
                            _fault_file.flush()
                    except Exception:  # noqa: BLE001
                        pass
                    hung = True
            else:
                hung = False

    threading.Thread(target=loop, name="watchdog", daemon=True).start()
