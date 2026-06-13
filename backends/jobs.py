"""Background generation queue.

A GenerationJob runs a backend call (the `runner` closure) on a worker thread and
drives the result row through pending -> generating -> done/failed, emitting Qt
signals the UI connects to. Hosted jobs run a few in parallel; local (ComfyUI) jobs
are serialized - one GPU. The runner is backend-agnostic: it takes a progress
callback and returns a dict that must include at least {'video_path': ...}.
"""
from __future__ import annotations

from datetime import datetime
from typing import Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot

from store.db import Store
from store.models import STATUS_DONE, STATUS_FAILED, STATUS_GENERATING

Runner = Callable[[Callable[[str], None]], dict]

# result fields a runner may return that we persist on success.
_RESULT_FIELDS = ("video_path", "fps", "frame_count", "cost_actual", "preview_gif", "thumbnail")


class _JobSignals(QObject):
    status_changed = Signal(str, str)   # result_id, status
    progress = Signal(str, str)         # result_id, line
    finished = Signal(str)              # result_id
    failed = Signal(str, str)           # result_id, error


class GenerationJob(QRunnable):
    def __init__(self, store: Store, result_id: str, backend: str, runner: Runner,
                 signals: _JobSignals):
        super().__init__()
        self.store = store
        self.result_id = result_id
        self.backend = backend
        self.runner = runner
        self.signals = signals

    @Slot()
    def run(self) -> None:
        rid = self.result_id
        self.store.update_result(rid, status=STATUS_GENERATING)
        self.signals.status_changed.emit(rid, STATUS_GENERATING)
        job = self.store.add_job(rid, backend=self.backend, state="running")

        log_lines: list[str] = []

        def progress(line: str) -> None:
            log_lines.append(line)
            self.signals.progress.emit(rid, line)
            self.store.update_job(job.id, log="\n".join(log_lines))

        try:
            result = self.runner(progress) or {}
            fields = {"status": STATUS_DONE,
                      "completed": datetime.now().isoformat(timespec="seconds")}
            for k in _RESULT_FIELDS:
                if k in result:
                    fields[k] = result[k]
            self.store.update_result(rid, **fields)
            self.store.update_job(job.id, state="done", ext_id=result.get("prediction_id"))
            self.signals.status_changed.emit(rid, STATUS_DONE)
            self.signals.finished.emit(rid)
        except Exception as e:  # noqa: BLE001 - surface any backend failure on the row
            err = f"{type(e).__name__}: {e}"
            self.store.update_result(rid, status=STATUS_FAILED, error=err)
            self.store.update_job(job.id, state="failed", log="\n".join(log_lines + [err]))
            self.signals.status_changed.emit(rid, STATUS_FAILED)
            self.signals.failed.emit(rid, err)


class JobManager(QObject):
    status_changed = Signal(str, str)
    progress = Signal(str, str)
    finished = Signal(str)
    failed = Signal(str, str)

    def __init__(self, store: Store, hosted_concurrency: int = 3):
        super().__init__()
        self.store = store
        self._signals = _JobSignals()
        self._signals.status_changed.connect(self.status_changed)
        self._signals.progress.connect(self.progress)
        self._signals.finished.connect(self.finished)
        self._signals.failed.connect(self.failed)

        self._hosted_pool = QThreadPool()
        self._hosted_pool.setMaxThreadCount(hosted_concurrency)
        self._local_pool = QThreadPool()
        self._local_pool.setMaxThreadCount(1)  # one GPU - serialize local renders

    def enqueue(self, result_id: str, backend: str, runner: Runner) -> None:
        job = GenerationJob(self.store, result_id, backend, runner, self._signals)
        pool = self._local_pool if backend == "comfyui" else self._hosted_pool
        pool.start(job)

    def active_count(self) -> int:
        return self._hosted_pool.activeThreadCount() + self._local_pool.activeThreadCount()

    def wait_for_done(self, msecs: int = -1) -> bool:
        ok = self._hosted_pool.waitForDone(msecs)
        return self._local_pool.waitForDone(msecs) and ok
