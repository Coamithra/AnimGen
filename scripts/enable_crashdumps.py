"""Enable a full native crash dump for the AnimGen process via Windows Error Reporting.

The 2026-06-18 crash was a NATIVE stack overflow (a Windows fatal exception). Python's
faulthandler can only dump the *Python* stack - which was shallow, because the real
recursion was in C/C++ (Qt). To see the native call stack that actually overflowed, have
the OS write a full minidump on the next crash, then open the .dmp in Visual Studio or
WinDbg and look at the faulting thread.

This configures WER LocalDumps for python.exe (the interpreter AnimGen runs under). While
enabled it applies to ALL python.exe crashes on the machine - fine for a focused
investigation; run with --disable to remove it afterwards. Requires Administrator
(LocalDumps lives under HKLM, which is the only hive WER reads).

  python scripts/enable_crashdumps.py            # enable (dumps -> data/crashdumps/)
  python scripts/enable_crashdumps.py --disable  # remove the WER config
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# WER reads LocalDumps only from HKLM (per-machine); HKCU is ignored.
_KEY = r"SOFTWARE\Microsoft\Windows\Windows Error Reporting\LocalDumps\python.exe"
_DUMP_DIR = Path(__file__).resolve().parent.parent / "data" / "crashdumps"


def main() -> int:
    if sys.platform != "win32":
        print("Windows-only (uses Windows Error Reporting LocalDumps).")
        return 1
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--disable", action="store_true", help="remove the WER LocalDumps config")
    args = ap.parse_args()
    import winreg

    try:
        if args.disable:
            try:
                winreg.DeleteKey(winreg.HKEY_LOCAL_MACHINE, _KEY)
                print("Disabled: removed WER LocalDumps for python.exe.")
            except FileNotFoundError:
                print("Already disabled (key not present).")
            return 0

        _DUMP_DIR.mkdir(parents=True, exist_ok=True)
        with winreg.CreateKey(winreg.HKEY_LOCAL_MACHINE, _KEY) as k:
            winreg.SetValueEx(k, "DumpFolder", 0, winreg.REG_EXPAND_SZ, str(_DUMP_DIR))
            winreg.SetValueEx(k, "DumpType", 0, winreg.REG_DWORD, 2)    # 2 = full minidump
            winreg.SetValueEx(k, "DumpCount", 0, winreg.REG_DWORD, 10)
        print(f"Enabled. Full python.exe minidumps -> {_DUMP_DIR}")
        print("Reproduce the crash, then open the newest .dmp in Visual Studio / WinDbg and")
        print("inspect the faulting thread's NATIVE call stack (that's the overflow site).")
        print("Run with --disable to remove this afterwards.")
        return 0
    except PermissionError:
        print("ERROR: needs Administrator (HKLM). Re-run from an elevated terminal:")
        print("  python scripts/enable_crashdumps.py")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
