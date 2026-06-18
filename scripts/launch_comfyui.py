"""Launch the local ComfyUI backend with the settings AnimGen requires (CLI).

The one hard requirement is `--disable-dynamic-vram`: ComfyUI's default dynamic-VRAM
engine (comfy-aimdo) streams weights RAM<->VRAM mid-kernel and, on the 12GB card,
stalls a 14B render past Windows' 2s GPU watchdog (TDR) -> driver reset -> ComfyUI dies
mid-job with no traceback. This cost the Fighter project a night of crashes (2026-06-14);
see ../Fighter/research/comfyui-gpu-watchdog-crash-and-aimdo.md. comfy_client.preflight()
refuses to run a local job against a server WITHOUT this flag.

The actual command is built by comfy_client.build_launch_command() (shared with the
in-app "Launch ComfyUI" button). This wrapper just runs it in the FOREGROUND so you can
watch the log in a terminal. For a detached/double-click launch use launch_comfyui.bat,
or the toolbar button in the app. Extra CLI args pass through verbatim.

    python scripts/launch_comfyui.py                 # 127.0.0.1:8188, dynamic VRAM off
    python scripts/launch_comfyui.py --port 8189     # extra args pass through
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backends import comfy_client  # noqa: E402


def main(argv: list[str]) -> int:
    if not (comfy_client.COMFY_DIR / "main.py").exists():
        print(f"ComfyUI not found at {comfy_client.COMFY_DIR} (set ANIMGEN_COMFY_DIR).",
              file=sys.stderr)
        return 2
    cmd = comfy_client.build_launch_command(argv)
    print("launching:", " ".join(cmd))
    # env redirects the CUDA kernel cache into data/gpu_cache (isolated + capped) - see
    # comfy_client.launch_env(); matches the detached "Launch ComfyUI" button + the .bat.
    return subprocess.call(cmd, cwd=str(comfy_client.COMFY_DIR), env=comfy_client.launch_env())


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
