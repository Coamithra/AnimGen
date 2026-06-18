"""Ad-hoc batch monitor for the overnight Biker run (session-scoped, not committed).

Polls takes.json + the ComfyUI port and exits (so the agent is re-invoked) on any
noteworthy event: a take finishes, a crash (comfy port drops), a take fails, the local
queue is abandoned, the batch fully drains, or a periodic check-in timeout.
"""
import json, socket, sys, time
from collections import Counter
from pathlib import Path

TAKES = Path("data/Biker.assets/takes.json")
BATCH_AFTER = "2026-06-18T11:00"   # batch takes created after this; orphans are older
COMFY_PORT = 8188
POLL = 15
CHECKIN_S = 1800   # periodic re-check-in even if nothing changes (~30 min)


def port_up(p):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(1.5)
    try:
        return s.connect_ex(("127.0.0.1", p)) == 0
    finally:
        s.close()


def load_batch():
    try:
        d = json.loads(TAKES.read_text())
    except Exception as e:
        return None, str(e)
    takes = d if isinstance(d, list) else d.get("takes", d)
    if isinstance(takes, dict):
        takes = list(takes.values())
    batch = [t for t in takes if (t.get("created") or "") >= BATCH_AFTER]
    return batch, None


def summarize(batch):
    c = Counter(t.get("status") for t in batch)
    return c


def main():
    start = time.time()
    prev = None
    prev_port = port_up(COMFY_PORT)
    crash_seen = 0
    while True:
        batch, err = load_batch()
        if err:
            print(f"[monitor] takes read error: {err}")
            time.sleep(POLL)
            continue
        c = summarize(batch)
        up = port_up(COMFY_PORT)
        n_total = len(batch)
        n_done = c.get("done", 0)
        n_gen = c.get("generating", 0)
        n_pend = c.get("pending", 0)
        n_fail = c.get("failed", 0)
        n_canc = c.get("cancelled", 0)
        terminal = (n_gen == 0 and n_pend == 0)

        # event detection
        events = []
        if prev_port and not up:
            crash_seen += 1
            events.append(f"COMFY PORT DROPPED (crash #{crash_seen}) - watch recovery")
        if not prev_port and up:
            events.append("comfy port back UP (restarted)")
        if prev is not None:
            if n_done > prev.get("done", 0):
                events.append(f"take(s) DONE: {prev.get('done',0)} -> {n_done}")
            if n_fail > prev.get("failed", 0):
                events.append(f"take FAILED: {prev.get('failed',0)} -> {n_fail}")
        # abandon heuristic: nothing in flight but not all done and some cancelled this run
        prev = dict(done=n_done, failed=n_fail, cancelled=n_canc, gen=n_gen, pend=n_pend)
        prev_port = up

        line = (f"[{time.strftime('%H:%M:%S')}] batch={n_total} done={n_done} gen={n_gen} "
                f"pend={n_pend} fail={n_fail} canc={n_canc} comfy={'up' if up else 'DOWN'}")
        if events:
            print(line)
            for e in events:
                print("   * " + e)
            sys.stdout.flush()
            # exit on crash, failure, or completion so the agent reacts
            if any("DROPPED" in e or "FAILED" in e for e in events):
                print("[monitor] EXIT: needs attention")
                return 2

        if terminal and n_total > 0:
            print(line)
            print(f"[monitor] EXIT: batch terminal (done={n_done} fail={n_fail} canc={n_canc})")
            return 0

        if time.time() - start > CHECKIN_S:
            print(line)
            print("[monitor] EXIT: periodic check-in")
            return 3

        time.sleep(POLL)


if __name__ == "__main__":
    sys.exit(main())
