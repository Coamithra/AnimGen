"""Map the hot recursion RVAs to nearest exported symbols (no PDBs available).

A return address sits *after* a call, inside some function; with only the export
table we report the nearest exported symbol at-or-below the RVA. Qt internals are
mostly non-exported, so this is a neighborhood hint, not an exact frame name.
"""
import pefile

TARGETS = {
    r".venv/Lib/site-packages/PySide6/Qt6Widgets.dll": [0x62a15],
    r".venv/Lib/site-packages/PySide6/Qt6Gui.dll": [0x33b7d4, 0x33eaa4, 0x3389ef, 0x33ea00],
}

for dll, rvas in TARGETS.items():
    pe = pefile.PE(dll, fast_load=True)
    pe.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_EXPORT"]])
    exports = []
    if hasattr(pe, "DIRECTORY_ENTRY_EXPORT"):
        for e in pe.DIRECTORY_ENTRY_EXPORT.symbols:
            if e.name:
                exports.append((e.address, e.name.decode("ascii", "replace")))
    exports.sort()
    print(f"\n==== {dll.split('/')[-1]}  ({len(exports)} named exports) ====")
    for rva in rvas:
        # nearest export at or below rva
        lo, hi = 0, len(exports)
        while lo < hi:
            mid = (lo + hi) // 2
            if exports[mid][0] <= rva:
                lo = mid + 1
            else:
                hi = mid
        i = lo - 1
        if 0 <= i < len(exports):
            addr, name = exports[i]
            nxt = exports[i + 1] if i + 1 < len(exports) else (None, "")
            print(f"  RVA 0x{rva:x}:")
            print(f"     >= export 0x{addr:x} (+0x{rva-addr:x})  {name}")
            if nxt[0] is not None:
                print(f"     <  next   0x{nxt[0]:x}            {nxt[1]}")
        else:
            print(f"  RVA 0x{rva:x}: (below first export)")
