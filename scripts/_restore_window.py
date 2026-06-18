"""Restore (un-minimize) and foreground the running AnimGen window via the Win32 API.

The app is alive but minimized; this finds its top-level window by the pid that owns the
remote-control port and ShowWindow(SW_RESTORE)s it. Read-only w.r.t. the app's state - pure
window management, no input injection.
"""
import ctypes
import subprocess
from ctypes import wintypes

PORT = 8765
SW_RESTORE = 9
user32 = ctypes.windll.user32


def app_pid():
    out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True).stdout
    for line in out.splitlines():
        if f":{PORT} " in line and "LISTENING" in line:
            return int(line.split()[-1])
    return None


def main():
    pid = app_pid()
    if not pid:
        print("could not find app pid on port", PORT)
        return
    print("app pid:", pid)
    matches = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _lparam):
        wpid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
        if wpid.value == pid and user32.GetWindowTextLengthW(hwnd) > 0:
            buf = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(hwnd, buf, 512)
            matches.append((hwnd, buf.value))
        return True

    user32.EnumWindows(cb, 0)
    if not matches:
        print("no titled top-level window for pid", pid, "(may be hidden, not minimized)")
        return
    for hwnd, title in matches:
        user32.ShowWindow(hwnd, SW_RESTORE)
        user32.SetForegroundWindow(hwnd)
        user32.BringWindowToTop(hwnd)
        print(f"restored + raised: '{title}' (hwnd={hwnd})")


if __name__ == "__main__":
    main()
