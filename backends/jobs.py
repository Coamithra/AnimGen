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

import shiboken6
from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot

from backends.crash_recovery import CRASH_INTERRUPTED_ATTR
from pipeline import extract
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
    queue_abandoned = Signal(str)       # reason - local queue paused after repeated crashes


class GenerationJob(QRunnable):
    def __init__(self, project: Project, take_id: str, backend: str, runner: Runner,
                 signals: _JobSignals, cancelled: set, stopping: set, requeue: set,
                 done_cb: Callable[[str, str], None]):
        super().__init__()
        self.project = project
        self.take_id = take_id
        self.backend = backend
        self.runner = runner
        self.signals = signals
        self.cancelled = cancelled   # shared with JobManager; ids cancelled while queued
        self.stopping = stopping     # shared; ids whose in-flight render was asked to stop
        self.requeue = requeue       # shared; ids whose in-flight render was halted to re-run
        self.done_cb = done_cb       # JobManager._on_job_done(take_id, final_status)

    def _emit(self, name: str, *args) -> None:
        """Emit a signal on self.signals, degrading to a no-op if its C++ QObject was deleted.

        run() executes on a worker thread; if the _JobSignals object is torn down (project /
        JobManager churn, GC) while a worker is still mid-render, an unguarded
        self.signals.<sig>.emit(...) raises RuntimeError('Signal source has been deleted').
        Because run() is a QRunnable override invoked from C++, that uncaught exception aborts
        the whole process at the C++ layer (std::terminate) with NO Python traceback - the
        crash this fix targets. shiboken6.isValid gates the common case; the try/except closes
        the race between the check and the emit. The take's state is already persisted via
        write-through (project.update_take), so a dropped signal only costs a UI refresh."""
        try:
            if not shiboken6.isValid(self.signals):
                return
            getattr(self.signals, name).emit(*args)
        except RuntimeError:
            pass

    def _cancel_remote_spend(self, tid: str, progress: Callable[..., None]) -> None:
        """Best-effort cancel of a hosted prediction after this take FAILED terminally mid-render.

        The H4 network retries (PR #93) mean a single blip no longer fails a hosted take, but a
        take that DOES fail terminally after the create POST returned a prediction id (poll retries
        exhausted, download error, an unwind, etc.) leaves the Replicate prediction running to
        completion at full cost with its output never claimed (card follow-up H4). If the take is a
        replicate one and recorded a `backend_job_id` (stamped mid-render by the runner's on_submit),
        POST a cancel to stop the remaining spend. This does NOT change the take's terminal status -
        it stayed FAILED above; the cancel only stops the bleed. A never-submitted take (no
        backend_job_id) and the local/comfyui backend are skipped. Called only from the genuine-
        terminal-failure branch, never for a deliberate stop (that's `_stopping`/`request_stop`,
        which already issued its own cancel) or a re-queue. Swallows all transport errors, like
        request_stop - a cancel that itself fails must never take down the worker. Re-fetches the
        take so it reads the id on_submit persisted mid-render, not a stale snapshot."""
        if self.backend != "replicate":
            return
        t = self.project.get_take(tid)
        pred_id = t.backend_job_id if t else None
        if not pred_id:
            return
        try:
            from backends import replicate_client
            replicate_client.cancel_prediction(pred_id)
            progress(f"requested cancel of prediction {pred_id} to stop remote spend (best-effort)")
        except Exception:  # noqa: BLE001 - best-effort; a failed cancel must not abort the worker
            pass

    @Slot()
    def run(self) -> None:
        # Belt-and-braces: a QRunnable::run() override that lets ANY exception escape propagates
        # into C++ as std::terminate/abort, killing the whole app with no traceback. Swallow
        # everything (BaseException deliberately - KeyboardInterrupt/SystemExit on a pool worker
        # would abort just as hard) so a single take's failure can never take the process down.
        try:
            self._run()
        except BaseException:  # noqa: BLE001 - never let anything escape the C++ override
            # _run's own finally normally records the take and calls done_cb. If it raised
            # *before* that finally (e.g. the GENERATING-transition update_take/add_job itself
            # failed), the take would be left non-terminal and its serialized-pool slot leaked -
            # the same "a take silently vanishes" failure mode this fix targets. Last resort:
            # record FAILED (best-effort) and fire done_cb so the manager still frees the slot.
            import traceback
            traceback.print_exc()
            try:
                self.project.update_take(self.take_id, status=STATUS_FAILED, error="worker aborted")
            except Exception:  # noqa: BLE001 - best-effort; never re-raise out of the override
                pass
            self.done_cb(self.take_id, STATUS_FAILED)

    def _run(self) -> None:
        tid = self.take_id
        if tid in self.cancelled:    # cancelled while queued - never invoke the backend
            self.project.update_take(tid, status=STATUS_CANCELLED, error="cancelled before start",
                                     interrupted=False)   # user cancel, not a crash
            self._emit("status_changed", tid, STATUS_CANCELLED)
            self.done_cb(tid, STATUS_CANCELLED)
            return
        self.project.update_take(tid, status=STATUS_GENERATING,
                                 started=datetime.now().isoformat(timespec="seconds"))
        self._emit("status_changed", tid, STATUS_GENERATING)
        job = self.project.add_job(tid, backend=self.backend, state="running")

        log_lines: list[str] = []
        final = STATUS_FAILED

        def progress(line: str | None = None, *, frac: float | None = None,
                     label: str = "") -> None:
            if line is not None:                 # milestone: log line + persist (infrequent)
                log_lines.append(line)
                self._emit("progress", tid, line)
                self.project.update_job(job.id, log="\n".join(log_lines))
            if frac is not None:                 # per-step fraction: UI bar only, no JSON write
                self._emit("progress_pct", tid, frac, label)

        try:
            result = self.runner(progress) or {}
            # No backend runner reports fps/frame_count, so probe the produced video to stamp
            # them on the take (consumed by export's settings.txt and the shot editor's
            # measured-fps readout). Best-effort - a probe failure leaves them None.
            if result.get("video_path"):
                result["fps"], result["frame_count"] = extract.probe_media_fields(
                    result["video_path"], result.get("fps"), result.get("frame_count"))
            fields = {"status": STATUS_DONE,
                      "completed": datetime.now().isoformat(timespec="seconds")}
            for k in _TAKE_FIELDS:
                if k in result:
                    fields[k] = result[k]
            self.project.update_take(tid, **fields)
            self.project.update_job(job.id, state="done", ext_id=result.get("prediction_id"))
            self._emit("status_changed", tid, STATUS_DONE)
            self._emit("finished", tid)
            final = STATUS_DONE
        except Exception as e:  # noqa: BLE001 - surface any backend failure on the take
            err = f"{type(e).__name__}: {e}"
            if tid in self.requeue:
                # The render was deliberately halted to be put back on the queue (Pause batch ->
                # "halt current & re-add"): reset to PENDING so a resume re-runs it, rather than
                # recording it terminally. The runner is kept (see _on_job_done) for the re-enqueue.
                # Clear `started` so the held take looks like any other PENDING one; the re-run
                # re-stamps it (so "done in X" reflects the attempt that actually completed).
                self.project.update_take(tid, status=STATUS_PENDING, error=None, started=None)
                self.project.update_job(job.id, state="cancelled",
                                        log="\n".join(log_lines + [err, "re-queued by pause"]))
                self._emit("status_changed", tid, STATUS_PENDING)
                final = STATUS_PENDING
            elif tid in self.stopping or tid in self.cancelled:
                # The backend error is here because we asked it to stop (shot deleted /
                # cancelled mid-render), not because the render failed - record CANCELLED.
                self.project.update_take(tid, status=STATUS_CANCELLED,
                                         error="stopped by user", interrupted=False)   # deliberate stop
                self.project.update_job(job.id, state="cancelled",
                                        log="\n".join(log_lines + [err]))
                self._emit("status_changed", tid, STATUS_CANCELLED)
                final = STATUS_CANCELLED
            else:
                # A genuine workflow error is interrupted=False, but crash recovery re-raises the
                # original render error (verbatim) after a successful final restart - the GPU
                # recovered yet this take's in-flight render was lost. It stamps the exception so
                # this take is flagged interrupted, letting the bulk "Restart interrupted takes"
                # action pick it up like its abandon_local'd siblings (rule #17, card #68).
                interrupted = bool(getattr(e, CRASH_INTERRUPTED_ATTR, False))
                self.project.update_take(tid, status=STATUS_FAILED, error=err, interrupted=interrupted)
                # Mutate log_lines (not a `log_lines + [err]` copy): _cancel_remote_spend's
                # progress() milestone below re-writes the job log from log_lines, so err must
                # be IN the list or that later write would drop the error from the persisted log.
                log_lines.append(err)
                self.project.update_job(job.id, state="failed", log="\n".join(log_lines))
                self._emit("status_changed", tid, STATUS_FAILED)
                self._emit("failed", tid, err)
                # After the emits: the cancel POST can sit in the 5xx backoff loop for a while,
                # and the UI should surface the failure immediately, not after the cancel returns.
                self._cancel_remote_spend(tid, progress)
                final = STATUS_FAILED
        finally:
            self.stopping.discard(tid)
            self.requeue.discard(tid)
            self.done_cb(tid, final)


class JobManager(QObject):
    status_changed = Signal(str, str)
    progress = Signal(str, str)
    progress_pct = Signal(str, float, str)
    finished = Signal(str)
    failed = Signal(str, str)
    queue_abandoned = Signal(str)       # reason - local queue paused after repeated crashes

    def __init__(self, project: Project, hosted_concurrency: int = 3):
        super().__init__()
        self.project = project
        self._signals = _JobSignals()
        self._signals.status_changed.connect(self.status_changed)
        self._signals.progress.connect(self.progress)
        self._signals.progress_pct.connect(self.progress_pct)
        self._signals.finished.connect(self.finished)
        self._signals.failed.connect(self.failed)
        self._signals.queue_abandoned.connect(self.queue_abandoned)

        self._hosted_pool = QThreadPool()
        self._hosted_pool.setMaxThreadCount(hosted_concurrency)
        self._local_pool = QThreadPool()
        self._local_pool.setMaxThreadCount(1)  # one GPU - serialize local renders
        # The sets/flag below are shared with worker threads (read in GenerationJob.run,
        # discarded in its finally) or read by crash recovery. They hold only take ids /
        # a bool and are mutated with bare set add/discard/in (GIL-atomic) - no RLock needed
        # (unlike project JSON state).
        self._cancelled: set[str] = set()      # take ids cancelled while still queued
        self._stopping: set[str] = set()       # take ids whose in-flight render we stopped
        self._requeue: set[str] = set()        # take ids halted mid-render to re-run on resume
        # Original runner per enqueued-but-unfinished take, so a paused take can be re-enqueued
        # with its exact closure (no settings_snapshot drift). Dropped once the take is terminal.
        self._runners: dict[str, tuple[str, Runner]] = {}
        self._local_paused = False             # user paused the local queue (read by crash recovery)

    def set_project(self, project: Project) -> None:
        """Point the queue at a newly opened/created project."""
        self.project = project

    def enqueue(self, take_id: str, backend: str, runner: Runner) -> None:
        self._runners[take_id] = (backend, runner)
        self._start(take_id, backend, runner)

    def restart_take(self, take_id: str, backend: str, runner: Runner) -> None:
        """Re-enqueue a previously-terminal take (a CANCELLED one being restarted in place).

        A cancelled take's id lingers in `_cancelled` (and possibly `_stopping`/`_requeue`),
        which would make the fresh GenerationJob bail straight to CANCELLED. Clear that stale
        membership first, then enqueue exactly like a new take. The caller flips the take's
        status back to PENDING before calling this. GIL-atomic set ops, like resume_local."""
        self._cancelled.discard(take_id)
        self._stopping.discard(take_id)
        self._requeue.discard(take_id)
        self.enqueue(take_id, backend, runner)

    def _start(self, take_id: str, backend: str, runner: Runner) -> None:
        job = GenerationJob(self.project, take_id, backend, runner, self._signals,
                            self._cancelled, self._stopping, self._requeue, self._on_job_done)
        pool = self._local_pool if backend == "comfyui" else self._hosted_pool
        pool.start(job)

    def _on_job_done(self, take_id: str, final_status: str) -> None:
        """Worker-thread callback fired when a job leaves run(). Drop the retained runner once
        the take is terminal; keep it when the take was reset to PENDING (halt-and-requeue) so
        resume_local can re-enqueue the same closure. GIL-atomic dict op, like the sets above."""
        if final_status != STATUS_PENDING:
            self._runners.pop(take_id, None)

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
        self._runners.pop(take_id, None)
        self.project.update_take(take_id, status=STATUS_CANCELLED, error="cancelled by user",
                                 interrupted=False)
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
                self._runners.pop(t.id, None)
                self.project.update_take(t.id, status=STATUS_CANCELLED,
                                         error="cancelled by user (shot deleted)", interrupted=False)
                self._signals.status_changed.emit(t.id, STATUS_CANCELLED)
                n += 1
        return n

    def is_stop_requested(self, take_id: str) -> bool:
        """Whether request_stop has flagged this take's in-flight render to stop.

        Read by the hosted runner's `on_submit` to close the create-POST window: a stop
        requested before the prediction id was recorded skips request_stop's cancel (no
        `backend_job_id` yet), so on_submit re-checks this right after recording the id and
        self-cancels. GIL-atomic membership read, like the other `_stopping` accesses.
        """
        return take_id in self._stopping

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
        # A hosted take whose create-POST hasn't returned has no backend_job_id yet, so the
        # replicate cancel below is skipped - but the runner's on_submit re-checks
        # is_stop_requested() right after recording the id and self-cancels, closing that
        # window. One narrow window remains: a take whose worker is already past the backend
        # call lands DONE (its file then orphaned when the shot is deleted - the separate
        # media-binning gap). Rare; the common case (a take mid-poll) stops cleanly.
        try:
            if backend == "comfyui":
                from backends import comfy_client
                # Target our own prompt id (L8) so we drop only this take's pending prompt,
                # not the whole server queue; None falls back to the blanket clear.
                comfy_client.stop_work(prompt_id=t.backend_job_id)
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
        self._local_paused = False   # cancelling the whole queue clears any user pause
        self._hosted_pool.clear()
        self._local_pool.clear()
        # include_deleted=True: a take binned while still PENDING is otherwise excluded from
        # every queue scan, so its runnable would fire the backend into a binned take (H2). The
        # bin path now cancels it up front; sweeping it here too is belt-and-braces.
        pending = [t for t in self.project.list_takes(include_deleted=True)
                   if t.status == STATUS_PENDING]
        for t in pending:
            self._cancelled.add(t.id)
            self._runners.pop(t.id, None)
            self.project.update_take(t.id, status=STATUS_CANCELLED, error="cancelled by user",
                                     interrupted=False)
            self._signals.status_changed.emit(t.id, STATUS_CANCELLED)
        return len(pending)

    def is_local_paused(self) -> bool:
        """Whether the user paused the local (ComfyUI) queue. Read by crash recovery's
        should_abort: while paused, a render failure is a deliberate stop, not a crash to
        restart. GIL-atomic bool read."""
        return self._local_paused

    def clear_local_pause(self) -> None:
        """Clear the local-queue pause flag without re-enqueuing anything.

        The non-batch deliberate-stop path (card #42): a manual ComfyUI stop pauses the local
        queue so crash recovery doesn't fight it, but there's no Resume UI for single takes -
        once the in-flight take has drained, MainWindow calls this to lift the transient pause
        so a later render recovers from a genuine crash normally. Distinct from resume_local,
        which also re-enqueues held takes. GIL-atomic bool write."""
        self._local_paused = False

    def stop_and_requeue(self, take_id: str) -> bool:
        """Halt an in-flight (GENERATING) local render and reset it to PENDING so a resume
        re-runs it (Pause batch -> "halt current & re-add"). Returns whether we acted.

        Flags the take in `_requeue` (so the worker resets it to PENDING, not terminal, when
        its poll loop unwinds) and interrupts the comfy prompt - `stop_work` leaves the server
        UP, so the render error is a clean workflow-style unwind, never misread as a crash.
        Local/comfyui only; the runner is kept for the re-enqueue. Best-effort backend call.
        Refuses a take already being stopped via request_stop (a deliberate cancel / shot-delete
        wins - we must not turn it back into a re-run)."""
        t = self.project.get_take(take_id)
        if (not t or t.status != STATUS_GENERATING or take_id in self._stopping
                or (t.settings_snapshot or {}).get("backend") != "comfyui"):
            return False
        self._requeue.add(take_id)
        try:
            from backends import comfy_client
            comfy_client.stop_work(prompt_id=t.backend_job_id)  # target our prompt (L8)
        except Exception:  # noqa: BLE001 - best-effort; the worker still unwinds to PENDING
            pass
        return True

    def pause_local(self, requeue_current: bool = False) -> list[str]:
        """Pause the local (ComfyUI) queue and return the take ids being held for resume.

        Sets the paused flag (so crash recovery won't fight a deliberate ComfyUI stop) and
        drops the not-yet-started local runnables from the pool, leaving their takes PENDING
        (NOT cancelled) so resume_local can re-enqueue them. The currently-GENERATING take is
        left to finish normally - unless `requeue_current`, in which case it's halted via
        stop_and_requeue and included in the held list (resume re-runs it from scratch).
        Hosted takes are untouched (separate pool, no crash/restart issue)."""
        self._local_paused = True
        self._local_pool.clear()
        # include_deleted=True: a take binned while PENDING must still be held (not dropped),
        # or its runnable is lost and it sticks PENDING forever after a resume (H2).
        held = [t.id for t in self.project.list_takes(include_deleted=True)
                if t.status == STATUS_PENDING
                and (t.settings_snapshot or {}).get("backend") == "comfyui"]
        if requeue_current:
            gen = next((t for t in self.project.list_takes(include_deleted=True)
                        if t.status == STATUS_GENERATING
                        and (t.settings_snapshot or {}).get("backend") == "comfyui"), None)
            if gen and self.stop_and_requeue(gen.id):
                held.append(gen.id)
        return held

    def resume_local(self, take_ids: list[str]) -> int:
        """Resume a paused local queue: re-enqueue each held take still PENDING, using its
        retained original runner, and clear the paused flag. Returns how many were re-enqueued.

        A held id no longer PENDING (cancelled meanwhile) or whose runner was dropped is
        skipped. Clears the flag first so re-enqueued takes run under normal crash recovery.

        A halt-and-requeued take whose worker is still unwinding to PENDING is safe to re-start:
        the local pool is serialized (one thread), so a re-enqueued job queues behind the
        still-finishing original and can't render concurrently with it (and the original is
        already past all its take-status writes - only _on_job_done runs in its finally)."""
        self._local_paused = False
        n = 0
        for tid in take_ids:
            t = self.project.get_take(tid)
            entry = self._runners.get(tid)
            if not t or t.status != STATUS_PENDING or entry is None:
                continue
            self._cancelled.discard(tid)
            self._start(tid, *entry)
            n += 1
        return n

    def abandon_local(self, reason: str) -> int:
        """Pause the local (ComfyUI) queue after repeated crashes; return how many were cancelled.

        Drops the not-yet-started local runnables and marks every still-pending COMFYUI take
        CANCELLED with `reason` (hosted takes are untouched - a dead GPU doesn't affect a
        Replicate render). Emits `queue_abandoned` so the UI can surface the pause. Called from
        the crash-recovery worker thread: `clear()` only drops *queued* runnables (not the
        active one that's abandoning), `update_take` is RLock-guarded, and the signal auto-queues
        to the GUI thread. The shared `_cancelled` set is the safety net for a runnable already
        dequeued when we cleared.
        """
        self._local_pool.clear()
        # include_deleted=True: sweep a take binned while PENDING too, or its runnable is left
        # queued and it sticks PENDING forever (H2).
        pending = [t for t in self.project.list_takes(include_deleted=True)
                   if t.status == STATUS_PENDING
                   and (t.settings_snapshot or {}).get("backend") == "comfyui"]
        for t in pending:
            self._cancelled.add(t.id)
            self._runners.pop(t.id, None)
            self.project.update_take(t.id, status=STATUS_CANCELLED, error=reason,
                                     interrupted=True)   # GPU-crash abandon, not a user cancel
            self._signals.status_changed.emit(t.id, STATUS_CANCELLED)
        self._signals.queue_abandoned.emit(reason)
        return len(pending)

    def wait_for_done(self, msecs: int = -1) -> bool:
        ok = self._hosted_pool.waitForDone(msecs)
        return self._local_pool.waitForDone(msecs) and ok
