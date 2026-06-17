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
next one proceeds - we never restart-loop on a legitimately broken workflow. A take that
crashes MAX_ATTEMPTS times raises QueueAbandoned so the caller pauses the whole local queue
(the GPU / server is legitimately broken - don't restart forever).

run_with_crash_recovery is dependency-injected (render / server_running / restart_server /
note / on_abandon / clock) so it's unit-tested headless with fakes - no real ComfyUI, no GPU,
no real sleeps.
"""
from __future__ import annotations

from typing import Callable

MAX_ATTEMPTS = 3   # total tries per take before the whole local queue is abandoned
CRASH_PROBES = 3   # times to reconfirm "server down" before treating a failure as a crash


class QueueAbandoned(RuntimeError):
    """A take crashed MAX_ATTEMPTS times (or the server couldn't be restarted); the caller
    should pause the local queue rather than keep restarting ComfyUI."""


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
    genuine workflow error (server still up) or a deliberate user stop. Raises QueueAbandoned
    after `max_attempts` crashes, or if a restart fails.
    """
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
                # on_abandon cancels the still-PENDING siblings; THIS take is still
                # GENERATING so it's untouched there and instead fails normally when the
                # QueueAbandoned below propagates to the worker's except handler.
                reason = (f"ComfyUI crashed {max_attempts}x on this take (last after "
                          f"{elapsed}); pausing the local queue. Check the GPU / ComfyUI "
                          "before re-Generating.")
                note(reason)
                on_abandon(reason)
                raise QueueAbandoned(reason) from exc
            note(f"comfy crashed - failed in {elapsed}, retrying "
                 f"(attempt {attempt + 1}/{max_attempts})")
            try:
                restart_server()
            except Exception as rexc:  # noqa: BLE001 - couldn't bring the server back -> abandon
                reason = f"ComfyUI restart failed ({rexc}); pausing the local queue."
                note(reason)
                on_abandon(reason)
                raise QueueAbandoned(reason) from rexc
    raise QueueAbandoned("crash recovery exhausted")  # unreachable (loop returns or raises)
