"""Watch the running AnimGen crash-test and exit (so the agent is re-invoked) on any
noteworthy event: the process dies, faulthandler writes a stack-overflow dump, a new
minidump lands, max_widgets climbs abnormally (the rule-#18 accumulator manifesting), the
batch drains, or a periodic check-in. Read-only polling of logs/ports/process list.
"""
import re, subprocess, sys, time
from pathlib import Path

FAULTS = Path("data/animgen_faults.log")
LOG = Path("data/animgen.log")
DUMPS = Path("data/crashdumps")
PORT = 8765
POLL = 30
WIDGET_ALERT = 2500         # above the legit ~940 (470-row queue); a real leak climbs toward ~6900
CHECKIN_S = 1500            # ~25 min periodic check-in


def app_pid():
    try:
        out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True, timeout=10).stdout
        for line in out.splitlines():
            if f":{PORT} " in line and "LISTENING" in line:
                return int(line.split()[-1])
    except Exception:
        pass
    return None


def pid_alive(pid):
    try:
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"], capture_output=True, text=True, timeout=10).stdout
        return str(pid) in out
    except Exception:
        return True


def latest_widgets():
    try:
        tail = LOG.read_text(encoding="utf-8", errors="replace").splitlines()[-40:]
    except Exception:
        return None, None, None
    mw, jp, gen = None, None, None
    for ln in reversed(tail):
        if mw is None:
            m = re.search(r"max_widgets=(\d+)\(([^)]*)", ln)
            if m:
                mw = (int(m.group(1)), m.group(2))
        m2 = re.search(r"jobs_pending=(\d+) generating=(\d+)", ln)
        if m2 and jp is None:
            jp, gen = int(m2.group(1)), int(m2.group(2))
        if mw and jp is not None:
            break
    return mw, jp, gen


def main():
    start = time.time()
    pid = app_pid()
    faults0 = FAULTS.stat().st_size if FAULTS.exists() else 0
    dumps0 = len(list(DUMPS.glob("*.dmp"))) if DUMPS.exists() else 0
    peak_widgets = 0
    seen_running = False
    print(f"[watch] start pid={pid} faults_size={faults0} dumps={dumps0}")
    while True:
        # 1) crash: faulthandler dump grew
        fsize = FAULTS.stat().st_size if FAULTS.exists() else 0
        if fsize > faults0:
            print(f"[watch] !!! FAULT DUMP GREW ({faults0}->{fsize}) — likely the stack overflow")
            return 3
        # 2) crash: new minidump
        dumps = len(list(DUMPS.glob("*.dmp"))) if DUMPS.exists() else 0
        if dumps > dumps0:
            print(f"[watch] !!! NEW MINIDUMP ({dumps0}->{dumps})")
            return 4
        # 3) process died
        if pid and not pid_alive(pid):
            print(f"[watch] !!! APP PROCESS {pid} GONE")
            return 2
        # 4) widget accumulator climbing
        mw, jp, gen = latest_widgets()
        if mw:
            peak_widgets = max(peak_widgets, mw[0])
            if mw[0] >= WIDGET_ALERT:
                print(f"[watch] !!! max_widgets={mw[0]} ({mw[1]}) — ACCUMULATOR CLIMBING")
                return 5
        # 5) batch drained
        if jp is not None:
            if (jp or 0) + (gen or 0) > 0:
                seen_running = True
            elif seen_running:
                print(f"[watch] batch drained (pending={jp} generating={gen}); no crash this run")
                return 0
        el = int(time.time() - start)
        print(f"[watch] {el}s  pid={pid}  pending={jp} gen={gen}  max_widgets={mw[0] if mw else '?'}"
              f"({mw[1] if mw else ''})  peak={peak_widgets}")
        sys.stdout.flush()
        if time.time() - start > CHECKIN_S:
            print("[watch] periodic check-in")
            return 6
        time.sleep(POLL)


if __name__ == "__main__":
    sys.exit(main())
