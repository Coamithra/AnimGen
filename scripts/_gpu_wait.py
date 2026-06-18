"""Wait until the GPU is free (the user's game is closed), then exit so the agent resumes.

Session-scoped scratch. Polls nvidia-smi; exits 0 once the GPU has been sustainedly idle
(low util + low VRAM) for a few consecutive checks, or exits 3 at a time cap for a check-in.
Does NOT touch the GPU itself - read-only polling so the running game is undisturbed.
"""
import subprocess, sys, time

POLL = 45
FREE_STREAK_NEEDED = 6      # ~4.5 min sustained idle before declaring the game closed
UTIL_FREE = 25             # % - desktop idle is low; a game pins ~90%+
MEM_FREE_MIB = 3500        # game holds ~7GB; desktop idle ~1-2GB
CAP_S = 1800               # 30 min -> check-in exit so the agent isn't blind indefinitely


def sample():
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=utilization.gpu,memory.used",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=20).stdout.strip().splitlines()
        util, mem = out[0].split(",")
        return int(util.strip()), int(mem.strip())
    except Exception as e:  # noqa: BLE001
        return None, None


def main():
    start = time.time()
    streak = 0
    while True:
        util, mem = sample()
        free = (util is not None and util < UTIL_FREE and mem < MEM_FREE_MIB)
        streak = streak + 1 if free else 0
        print(f"[{time.strftime('%H:%M:%S')}] gpu_util={util}% vram={mem}MiB "
              f"{'FREE' if free else 'busy'} streak={streak}/{FREE_STREAK_NEEDED}")
        sys.stdout.flush()
        if streak >= FREE_STREAK_NEEDED:
            print("[gpu_wait] EXIT: GPU sustainedly idle - game looks closed, resume batch")
            return 0
        if time.time() - start > CAP_S:
            print("[gpu_wait] EXIT: 30-min check-in (still busy)")
            return 3
        time.sleep(POLL)


if __name__ == "__main__":
    sys.exit(main())
