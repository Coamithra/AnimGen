"""Session-scoped minidump cracker for the 03:34 STATUS_STACK_OVERFLOW dump.

No symbols needed: for a stack overflow the recursive return-address repeats
thousands of times on the blown stack. We scan the faulting thread's captured
stack, classify every 8-byte slot by module, and report the dominant repeating
return addresses (as module+offset) + the recursion stride (frame size).
"""
import sys, struct
from collections import Counter
from minidump.minidumpfile import MinidumpFile

DUMP = sys.argv[1] if len(sys.argv) > 1 else r"data/crashdumps/python.exe.42392.0334.stackoverflow.dmp"

mf = MinidumpFile.parse(DUMP)
reader = mf.get_reader()

# ---- exception ----
exc = mf.exception
exc_tid = getattr(exc, "ThreadId", None)
rec = getattr(exc, "ExceptionRecord", None)
exc_code = getattr(rec, "ExceptionCode", None) if rec else None
exc_addr = getattr(rec, "ExceptionAddress", None) if rec else None
print("=== EXCEPTION ===")
print(f"  faulting ThreadId : {exc_tid} (0x{exc_tid:x})" if exc_tid is not None else "  (no exception stream)")
print(f"  ExceptionCode     : {hex(exc_code) if exc_code is not None else '?'}")
print(f"  ExceptionAddress  : {hex(exc_addr) if exc_addr is not None else '?'}")

# ---- modules ----
mods = []
for m in mf.modules.modules:
    base = m.baseaddress
    size = m.size
    name = m.name.split("\\")[-1]
    mods.append((base, base + size, name))
mods.sort()
print(f"\n=== MODULES ({len(mods)}) ===")

def classify(v):
    lo, hi = 0, len(mods)
    while lo < hi:
        mid = (lo + hi) // 2
        if mods[mid][0] <= v:
            lo = mid + 1
        else:
            hi = mid
    i = lo - 1
    if 0 <= i < len(mods) and mods[i][0] <= v < mods[i][1]:
        return mods[i]
    return None

for b, e, n in mods:
    if any(k in n.lower() for k in ("qt6", "python", "pyside", "shiboken")):
        print(f"  {b:#018x} - {e:#018x}  {n}")

# ---- faulting thread stack ----
fault_thread = None
for t in mf.threads.threads:
    if t.ThreadId == exc_tid:
        fault_thread = t
        break
if fault_thread is None:
    print("\n!! faulting thread not found in thread list; falling back to thread[0]")
    fault_thread = mf.threads.threads[0]

stack = fault_thread.Stack
start = stack.StartOfMemoryRange
size = stack.MemoryLocation.DataSize
print(f"\n=== FAULTING THREAD STACK ===")
print(f"  tid={fault_thread.ThreadId} start={start:#x} size={size} ({size/1024:.0f} KiB)")

try:
    blob = reader.read(start, size)
except TypeError:
    br = reader.get_buffered_reader()
    br.move(start)
    blob = br.read(size)
print(f"  read {len(blob)} bytes")

# scan 8-byte aligned slots
per_module = Counter()
addr_counts = Counter()
offsets_of_top = []
n_slots = len(blob) // 8
vals = struct.unpack_from(f"<{n_slots}Q", blob, 0)
for idx, v in enumerate(vals):
    m = classify(v)
    if m:
        per_module[m[2]] += 1
        addr_counts[v] += 1

print(f"\n=== STACK SLOTS POINTING INTO A LOADED MODULE ===")
for name, cnt in per_module.most_common(12):
    print(f"  {cnt:7d}  {name}")

print(f"\n=== TOP REPEATING RETURN ADDRESSES (recursion sites) ===")
for v, cnt in addr_counts.most_common(15):
    m = classify(v)
    off = v - m[0]
    print(f"  {cnt:7d} x  {m[2]}+0x{off:x}   (va {v:#x})")

# recursion stride for the single most common address
if addr_counts:
    topv, topc = addr_counts.most_common(1)[0]
    positions = [start + i * 8 for i, v in enumerate(vals) if v == topv]
    strides = [positions[i + 1] - positions[i] for i in range(min(len(positions) - 1, 40))]
    sc = Counter(strides)
    print(f"\n=== RECURSION STRIDE for top address ({topc} occurrences) ===")
    for s, c in sc.most_common(6):
        print(f"  stride {s} bytes  x{c}")
