"""Background generation queue.

A GenerationJob runs a backend call (the `runner` closure) on a worker thread and
drives the take through pending -> generating -> done/failed, emitting Qt signals the
UI connects to. Hosted jobs run a few in parallel; local (ComfyUI) jobs are serialized
- one GPU. The runner is backend-agnostic: it takes a progress callback and returns a
dict that must include at least {'video_path': ...}.

Take updates write through to the project's takes.json (in Project.update_take), so a
finished render survives even before the user saves the project.
"""
from __future__ import annotations

from datetime import datetime
from typing import Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot

from store.project import Project
from store.models import (
    STATUS_CANCELLED, STATUS_DONE, STATUS_FAILED, STATUS_GENERATING, STATUS_PENDING,
)

# A runner is handed a progress callback: progress(line) logs a milestone (persisted),
# progress(frac=.., label=..) reports a 0..1 completion fraction for the UI bar (ephemeral).
Runner = Callable[[Callable[..., None]], dict]

# take fields a runner may return that we persist on success.
_TAKE_FIELDS = ("video_path", "fps", "frame_count", "cost_actual", "preview_gif", "thumbnail")


class _JobSignals(QObject):
    status_changed = Signal(str, str)   # take_id, status
    progress = Signal(str, str)         # take_id, line
    progress_pct = Signal(str, float, str)  # take_id, fraction 0..1, label (ephemeral, UI-only)
    finished = Signal(str)              # take_id
    failed = Signal(str, str)           # take_id, error


class GenerationJob(QRunnable):
    def __init__(self, project: Project, take_id: str, backend: str, runner: Runner,
                 signals: _JobSignals, cancelled: set, stopping: set):
        super().__init__()
        self.project = project
        self.take_id = take_id
        self.backend = backend
        self.runner = runner
        self.signals = signals
        self.cancelled = cancelled   # shared with JobManager; ids cancelled while queued
        self.stopping = stopping     # shared; ids whose in-flight render was asked to stop

    @Slot()
    def run(self) -> None:
        tid = self.take_id
        if tid in self.cancelled:    # cancelled while queued - never invoke the backend
            self.project.update_take(tid, status=STATUS_CANCELLED, error="cancelled before start")
            self.signals.status_changed.emit(tid, STATUS_CANCELLED)
            return
        self.project.update_take(tid, status=STATUS_GENERATING)
        self.signals.status_changed.emit(tid, STATUS_GENERATING)
        job = self.project.add_job(tid, backend=self.backend, state="running")

        log_lines: list[str] = []

        def progress(line: str | None = None, *, frac: float | None = None,
                     label: str = "") -> None:
            if line is not None:                 # milestone: log line + persist (infrequent)
                log_lines.append(line)
                self.signals.progress.emit(tid, line)
                self.project.update_job(job.id, log="\n".join(log_lines))
            if frac is not None:                 # per-step fraction: UI bar only, no JSON write
                self.signals.progress_pct.emit(tid, frac, label)

        try:
            result = self.runner(progress) or {}
            fields = {"status": STATUS_DONE,
                      "completed": datetime.now().isoformat(timespec="seconds")}
            for k in _TAKE_FIELDS:
                if k in result:
                    fields[k] = result[k]
            self.project.update_take(tid, **fields)
            self.project.update_job(job.id, state="done", ext_id=result.get("prediction_id"))
            self.signals.status_changed.emit(tid, STATUS_DONE)
            self.signals.finished.emit(tid)
        except Exception as e:  # noqa: BLE001 - surface any backend failure on the take
            err = f"{type(e).__name__}: {e}"
            if tid in self.stopping or tid in self.cancelled:
                # The backend error is here because we asked it to stop (shot deleted /
                # cancelled mid-render), not because the render failed - record CANCELLED.
                self.project.update_take(tid, status=STATUS_CANCELLED,
                                         error="stopped by user")
                self.project.update_job(job.id, state="cancelled",
                                        log="\n".join(log_lines + [err]))
                self.signals.status_changed.emit(tid, STATUS_CANCELLED)
            else:
                self.project.update_take(tid, status=STATUS_FAILED, error=err)
                self.project.update_job(job.id, state="failed", log="\n".join(log_lines + [err]))
                self.signals.status_changed.emit(tid, STATUS_FAILED)
                self.signals.failed.emit(tid, err)
        finally:
            self.stopping.discard(tid)


class JobManager(QObject):
    status_changed = Signal(str, str)
    progress = Signal(str, str)
    progress_pct = Signal(str, float, str)
    finished = Signal(str)
    failed = Signal(str, str)

    def __init__(self, project: Project, hosted_concurrency: int = 3):
        super().__init__()
        self.project = project
        self._signals = _JobSignals()
        self._signals.status_changed.connect(self.status_changed)
        self._signals.progress.connect(self.progress)
        self._signals.progress_pct.connect(self.progress_pct)
        self._signals.finished.connect(self.finished)
        self._signals.failed.connect(self.failed)

        self._hosted_pool = QThreadPool()
        self._hosted_pool.setMaxThreadCount(hosted_concurrency)
        self._local_pool = QThreadPool()
        self._local_pool.setMaxThreadCount(1)  # one GPU - serialize local renders
        # Both sets are shared with worker threads (read in GenerationJob.run, discarded in
        # its finally). They hold only take ids and are mutated with bare set add/discard/in,
        # which CPython's GIL makes atomic - no RLock needed (unlike project JSON state).
        self._cancelled: set[str] = set()      # take ids cancelled while still queued
        self._stopping: set[str] = set()       # take ids whose in-flight render we stopped

    def set_project(self, project: Project) -> None:
        """Point the queue at a newly opened/created project."""
        self.project = project

    def enqueue(self, take_id: str, backend: str, runner: Runner) -> None:
        job = GenerationJob(self.project, take_id, backend, runner, self._signals,
                            self._cancelled, self._stopping)
        pool = self._local_pool if backend == "comfyui" else self._hosted_pool
        pool.start(job)

    def active_count(self) -> int:
        return self._hosted_pool.activeThreadCount() + self._local_pool.activeThreadCount()

    def pending_count(self) -> int:
        """Generations that are queued but haven't started rendering yet."""
        return sum(1 for t in self.project.list_takes() if t.status == STATUS_PENDING)

    def cancel_take(self, take_id: str) -> bool:
        """Cancel one queued-but-unstarted generation; return whether it was cancellable.

        Only a still-PENDING take can be cancelled here (mark it CANCELLED now and add it
        to the shared `_cancelled` set so the runnable skips the backend when its slot
        comes up). A take that's already GENERATING isn't pending - it's mid-render and
        must be stopped via the backend's own Stop control, so this returns False for it.
        """
        t = self.project.get_take(take_id)
        if not t or t.status != STATUS_PENDING:
            return False
        self._cancelled.add(take_id)
        self.project.update_take(take_id, status=STATUS_CANCELLED, error="cancelled by user")
        self._signals.status_changed.emit(take_id, STATUS_CANCELLED)
        return True

    def cancel_shot_takes(self, shot_id: str) -> int:
        """Cancel every still-PENDING take of one shot; return how many were cancelled.

        Used when a shot is deleted: its queued takes would otherwise fire the backend
        after the shot is gone and orphan their .mp4 in .assets/takes/. Only PENDING takes
        are touched here - a GENERATING one is mid-render and must be stopped via
        `request_stop`. Other shots' queued takes are left alone.
        """
        n = 0
        for t in self.project.list_takes(shot_id, include_deleted=True):
            if t.status == STATUS_PENDING:
                self._cancelled.add(t.id)
                self.project.update_take(t.id, status=STATUS_CANCELLED,
                                         error="cancelled by user (shot deleted)")
                self._signals.status_changed.emit(t.id, STATUS_CANCELLED)
                n += 1
        return n

    def request_stop(self, take_id: str) -> bool:
        """Stop an in-flight (GENERATING) render and return whether we acted.

        Flags the take in `_stopping` (so the worker records CANCELLED, not FAILED, when
        its poll loop unwinds) and issues the backend-side stop so spend/GPU actually
        halts: ComfyUI's interrupt for a local prompt, Replicate's cancel for a hosted
        prediction. The backend call is best-effort - a server that's already down or a
        prediction that already finished must not raise out of a delete. Imports the
        backends lazily to keep this module import-light.
        """
        t = self.project.get_take(take_id)
        if not t or t.status != STATUS_GENERATING:
            return False
        self._stopping.add(take_id)
        backend = (t.settings_snapshot or {}).get("backend")
        # Cooperative cancellation has two narrow windows we can't fully close here: a take
        # whose worker is already past the backend call lands DONE (its file then orphaned
        # when the shot is deleted - the separate media-binning gap), and a hosted take
        # whose create-POST hasn't returned has no backend_job_id yet, so no cancel is sent
        # and a successful prediction keeps spending. Both are rare; the common case (a
        # take mid-poll) stops cleanly.
        try:
            if backend == "comfyui":
                from backends import comfy_client
                comfy_client.stop_work()
            elif backend == "replicate" and t.backend_job_id:
                from backends import replicate_client
                replicate_client.cancel_prediction(t.backend_job_id)
        except Exception:  # noqa: BLE001 - best-effort; the worker still unwinds to CANCELLED
            pass
        return True

    def cancel_pending(self) -> int:
        """Cancel every queued-but-unstarted generation and return how many were cancelled.

        Drops the not-yet-started runnables from both pools and marks each still-pending
        take CANCELLED (immediate, so the UI updates now rather than only when the queue
        eventually drains). The in-progress job, if any, is GENERATING (not pending) and is
        left running - cancel that one with the backend's Stop control. The shared
        `_cancelled` set is a safety net for a runnable already dequeued when we cleared.
        """
        self._hosted_pool.clear()
        self._local_pool.clear()
        pending = [t for t in self.project.list_takes(include_deleted=False)
                   if t.status == STATUS_PENDING]
        for t in pending:
            self._cancelled.add(t.id)
            self.project.update_take(t.id, status=STATUS_CANCELLED, error="cancelled by user")
            self._signals.status_changed.emit(t.id, STATUS_CANCELLED)
        return len(pending)

    def wait_for_done(self, msecs: int = -1) -> bool:
        ok = self._hosted_pool.waitForDone(msecs)
        return self._local_pool.waitForDone(msecs) and ok
