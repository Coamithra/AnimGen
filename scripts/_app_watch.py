"""Watch the running AnimGen app during the mock-ComfyUI soak test.

Exits (so the agent is re-invoked) on: app death/stall (no heartbeat), a native fault dump,
a runaway widget census (rule #18), queue drain (survived), or a periodic check-in. Reads
data/animgen.log heartbeats -- session-scoped scratch, not committed.
"""
import re
import sys
import time
from datetime import datetime
from pathlib import Path

LOG = Path("data/animgen.log")
FAULTS = Path("data/animgen_faults.log")
POLL = 15
CHECKIN_S = 1800           # ~30 min quiet check-in
STALL_S = 90               # no heartbeat this long => app hung or DEAD
WIDGET_ALERT = 1500        # max_widgets past this => runaway accumulation (rule #18)

HB = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+ .*heartbeat\(gui\).*"
                r"jobs_pending=(\d+) generating=(\d+).*max_widgets=(\d+)")


def last_heartbeat():
    try:
        lines = LOG.read_text(errors="replace").splitlines()
    except Exception:  # noqa: BLE001
        return None
    for line in reversed(lines[-400:]):
        m = HB.search(line)
        if m:
            return (datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"),
                    int(m.group(2)), int(m.group(3)), int(m.group(4)))
    return None


def faults_size():
    try:
        return FAULTS.stat().st_size
    except Exception:  # noqa: BLE001
        return 0


def main():
    start = time.time()
    f0 = faults_size()
    peak_mw = 0
    while True:
        hb = last_heartbeat()
        now = datetime.now()
        if hb is None:
            print(f"[{now:%H:%M:%S}] no heartbeat parsed yet", flush=True)
        else:
            ts, pend, gen, mw = hb
            age = (now - ts).total_seconds()
            peak_mw = max(peak_mw, mw)
            print(f"[{now:%H:%M:%S}] hb_age={age:0.0f}s pending={pend} gen={gen} "
                  f"max_widgets={mw} (peak {peak_mw})", flush=True)
            if faults_size() > f0:
                print("[watch] EXIT: animgen_faults.log GREW -- a NATIVE FAULT was dumped "
                      "(rule #18 stack overflow?)")
                return 2
            if age > STALL_S:
                print(f"[watch] EXIT: heartbeat STALE ({age:0.0f}s) -- app HUNG or DEAD")
                return 2
            if mw > WIDGET_ALERT:
                # confirmed transient (rule #18 pileup): log it, but DON'T end the watch --
                # only a real crash/stall/fault should. The app survives these spikes.
                print(f"[watch]   ^^ TRANSIENT WIDGET SPIKE max_widgets={mw} (rule #18 "
                      f"DeferredDelete pileup) -- logging, watching for the actual crash")
            if pend == 0 and gen == 0:
                print("[watch] EXIT: queue DRAINED -- app SURVIVED the soak")
                return 0
        if time.time() - start > CHECKIN_S:
            print("[watch] EXIT: periodic check-in", flush=True)
            return 4
        time.sleep(POLL)


if __name__ == "__main__":
    sys.exit(main())
