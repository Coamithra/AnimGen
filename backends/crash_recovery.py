"""Crash detection + auto-restart for live local (ComfyUI) renders.

The 12GB card's 2s GPU watchdog (TDR) can still occasionally fire on a 14B render even with
the preventive `--disable-dynamic-vram` flag, killing the ComfyUI *process* mid-render - the
take's poll loop (comfy_client._poll_until_done) then sees the server unreachable and raises.
Because the local pool is serialized (one GPU, one worker), every remaining queued take would
otherwise fail one-by-one on preflight. This wraps a single local render so a *crash* restarts
ComfyUI and retries the SAME take in place, while the rest of the queue simply waits behind it
on that one serialized worker - so "requeue the rest" is automatic, nothing is re-enqueued.

A failure with the server still UP is a genuine workflow error (bad node, OOM that returns an
execution_error, ...), NOT a crash: it propagates unchanged so only that take fails and the
next one proceeds - we never restart-loop on a legitimately broken workflow. On the final
strike we try ONE last restart + responsiveness probe before giving up: a take crashes
MAX_ATTEMPTS times and the server still can't be brought back -> QueueAbandoned so the caller
pauses the whole local queue (the GPU / server is legitimately broken - don't restart
forever); but if that final restart recovers the server, only this take fails (the GPU was
only transiently down, so don't waste the rest of an overnight batch).

run_with_crash_recovery is dependency-injected (render / server_running / restart_server /
note / on_abandon / clock) so it's unit-tested headless with fakes - no real ComfyUI, no GPU,
no real sleeps.
"""
from __future__ import annotations

from typing import Callable

MAX_ATTEMPTS = 3   # total tries per take before the whole local queue is abandoned
CRASH_PROBES = 3   # times to reconfirm "server down" before treating a failure as a crash

# Stamped on a render exception re-raised after a SUCCESSFUL final restart (the GPU recovered, but
# this take's in-flight render was already lost to the crash), AND on the QueueAbandoned raised when
# the queue is abandoned (a restart failed / the server stayed down - see _abandon). In both cases
# the take in the worker IS the crash victim. The worker (backends.jobs.GenerationJob) reads this via
# getattr to record the take FAILED + interrupted=True, so the bulk "Restart interrupted takes" action
# picks it up alongside its abandon_local'd siblings - it WAS crash-killed, not a genuine workflow
# error (rule #17, cards #68 + #71). An attribute (vs a wrapper exception) keeps the original error
# message verbatim on the recovered path.
CRASH_INTERRUPTED_ATTR = "animgen_crash_interrupted"


class QueueAbandoned(RuntimeError):
    """A take crashed MAX_ATTEMPTS times (or the server couldn't be restarted); the caller
    should pause the local queue rather than keep restarting ComfyUI."""


def _abandon(note: Callable[[str], None], on_abandon: Callable[[str], None],
             reason: str) -> QueueAbandoned:
    """Log + pause the local queue, then build the QueueAbandoned for the caller to raise.

    The returned exception is stamped CRASH_INTERRUPTED_ATTR so the worker
    (backends.jobs.GenerationJob) records the crash-VICTIM take FAILED + interrupted=True: it
    crashed MAX_ATTEMPTS times and the server couldn't be brought back, so it WAS crash-killed,
    not a genuine workflow error - the bulk "Restart interrupted takes" then picks it up alongside
    the still-PENDING siblings on_abandon cancels with the same flag (card #71). The caller keeps
    `raise _abandon(...) from <cause>` so the original cause still chains through."""
    note(reason)
    on_abandon(reason)
    exc = QueueAbandoned(reason)
    setattr(exc, CRASH_INTERRUPTED_ATTR, True)
    return exc


def _looks_crashed(server_running: Callable[[], bool], probes: int) -> bool:
    """Did the render fail because ComfyUI *crashed* (server down) or because of a genuine
    *workflow error* (server up)?

    A single post-failure probe can race a transient blip - a momentarily slow `/system_stats`
    on a still-alive server would be misread as a crash and trigger a spurious restart. So a
    "down" reading is reconfirmed up to `probes` times before we commit to a restart. The first
    "up" reading is trusted immediately and returns False, which keeps the common workflow-error
    path to a single probe (no slowdown). On a genuinely down server each probe already blocks a
    full socket timeout (SYNs to the closed port are dropped, not refused - see CLAUDE.md), so
    the reconfirmation is naturally spaced without an explicit sleep."""
    for _ in range(max(1, probes)):
        if server_running():
            return False     # server answered -> genuine workflow error, not a crash
    return True              # down on every probe -> crashed


def format_elapsed(secs: int) -> str:
    """Seconds -> compact human span: 45 -> "45s", 75 -> "1m15s", 3675 -> "1h1m15s".

    Matches ui.queue_view._elapsed's style. Negative/garbage clamps to "0s"."""
    secs = max(0, int(secs))
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m}m{s}s"
    if m:
        return f"{m}m{s}s"
    return f"{s}s"


def run_with_crash_recovery(
    *,
    render: Callable[[], dict],
    server_running: Callable[[], bool],
    restart_server: Callable[[], None],
    note: Callable[[str], None],
    on_abandon: Callable[[str], None],
    clock: Callable[[], float],
    should_abort: Callable[[], bool] = lambda: False,
    max_attempts: int = MAX_ATTEMPTS,
    crash_probes: int = CRASH_PROBES,
) -> dict:
    """Run one local render with crash detection + auto-restart + retry.

    render          -> the render result dict; raises on any failure.
    server_running  -> True if ComfyUI is up *right now* (the crash signal: down == crashed).
                       Reconfirmed up to `crash_probes` times on a "down" reading before we
                       commit to a restart (see _looks_crashed); a single "up" is trusted.
    restart_server  -> stop + relaunch ComfyUI, blocking until responsive; raises if it can't.
    note            -> log a milestone line (surfaced on the take in the queue + the main log).
    on_abandon      -> pause the rest of the local queue with this reason (called once, before
                       the QueueAbandoned raise).
    clock           -> time source (time.time); injected so tests don't sleep.
    should_abort    -> True if the user deliberately stopped the local queue (paused a batch /
                       shut ComfyUI down by hand). A render failure then is NOT a crash to be
                       restarted - it's the intended stop, so re-raise the failure verbatim
                       (the worker records the take terminally / requeues it) and do NOT
                       restart or abandon. Default never-abort preserves the old behaviour.

    Returns render()'s result on success. Re-raises render()'s exception verbatim for a
    genuine workflow error (server still up), a deliberate user stop, or the final crash strike
    when one last restart brings ComfyUI back (only this take fails; the queue keeps running).
    Raises QueueAbandoned only when a restart fails or the server stays unreachable - so the
    last strike is decided on the server's actual state, not on the raw attempt count alone.
    """
    # At least one attempt, or the loop below never runs and we fall through to the trailing
    # `raise QueueAbandoned` WITHOUT routing through _abandon - i.e. without calling on_abandon
    # or stamping CRASH_INTERRUPTED_ATTR, breaking the abandon contract (the caller wouldn't
    # pause the queue, the crash victim wouldn't be flagged interrupted). max_attempts < 1 is
    # caller misuse (MAX_ATTEMPTS is 3), so assert it up front rather than special-case it.
    assert max_attempts >= 1, "max_attempts must be >= 1"
    for attempt in range(1, max_attempts + 1):
        t0 = clock()
        try:
            return render()
        except Exception as exc:  # noqa: BLE001 - any render failure is inspected below
            if should_abort():
                raise                      # user deliberately stopped -> don't restart/retry
            if not _looks_crashed(server_running, crash_probes):
                raise                      # server alive -> genuine workflow error, not a crash
            elapsed = format_elapsed(int(clock() - t0))
            if attempt >= max_attempts:
                # Last strike. Don't condemn the whole local queue on attempt count alone:
                # the GPU may have only transiently dropped, and one more restart can bring
                # ComfyUI back. Try exactly one final restart + responsiveness probe; abandon
                # the queue ONLY if that restart fails or the server stays unreachable. If it
                # recovers, fail just THIS take (re-raise) and let the rest of the local queue
                # keep rendering - it would otherwise be a wasted overnight run on a GPU that
                # was only briefly down. on_abandon cancels the still-PENDING siblings; a
                # re-raised render error instead fails only this GENERATING take in the worker.
                note(f"ComfyUI crashed {max_attempts}x on this take (last after {elapsed}); "
                     "attempting a final restart before pausing the local queue.")
                try:
                    restart_server()
                except Exception as rexc:  # noqa: BLE001 - couldn't bring it back -> abandon
                    reason = f"ComfyUI restart failed ({rexc}); pausing the local queue."
                    raise _abandon(note, on_abandon, reason) from rexc
                # The production restart_server (comfy_client) already blocks until responsive
                # and raises if it can't, so this re-probe normally confirms "up" immediately;
                # it abandons only if the server died again in the gap after the restart returned
                # (and on the injected fakes that don't block-until-up).
                if _looks_crashed(server_running, crash_probes):
                    reason = (f"ComfyUI still unreachable after a final restart (crashed "
                              f"{max_attempts}x on this take); pausing the local queue. "
                              "Check the GPU / ComfyUI before re-Generating.")
                    raise _abandon(note, on_abandon, reason) from exc
                note("ComfyUI recovered after a final restart; failing this take but "
                     "keeping the local queue running.")
                try:
                    setattr(exc, CRASH_INTERRUPTED_ATTR, True)  # crash-killed -> bulk-restartable
                except Exception:  # noqa: BLE001 - an exotic exception type may reject attrs
                    pass
                raise
            note(f"comfy crashed - failed in {elapsed}, retrying "
                 f"(attempt {attempt + 1}/{max_attempts})")
            try:
                restart_server()
            except Exception as rexc:  # noqa: BLE001 - couldn't bring the server back -> abandon
                reason = f"ComfyUI restart failed ({rexc}); pausing the local queue."
                raise _abandon(note, on_abandon, reason) from rexc
    # Unreachable: max_attempts >= 1 (asserted above) guarantees >= 1 iteration, and every
    # iteration returns or raises. Kept as a defensive backstop; it does NOT route through
    # _abandon because it can never fire.
    raise QueueAbandoned("crash recovery exhausted")
