"""Clear videogen's GPU (CUDA JIT kernel) shader cache.

By default wipes AnimGen's isolated, project-local cache (data/gpu_cache - where
comfy_client.launch_env() points a launched ComfyUI's CUDA_CACHE_PATH). With --all it ALSO
clears the GLOBAL pre-isolation NVIDIA ComputeCache(s) that kernels landed in before the
redirect existed (%APPDATA%/NVIDIA/ComputeCache, ~/.nv/ComputeCache). Best-effort: a file a
running ComfyUI holds open is skipped (CUDA just recompiles it on demand).

This is the CUDA *compute* cache only - it is NOT the DirectX DXCache (that one is filled by
desktop/browser/Electron apps, not videogen; clear it via Disk Cleanup / NVIDIA Control Panel).

    python scripts/clear_gpu_cache.py            # isolated data/gpu_cache only
    python scripts/clear_gpu_cache.py --all      # + the global NVIDIA ComputeCache(s)
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backends import comfy_client  # noqa: E402


def main(argv: list[str]) -> int:
    include_legacy = "--all" in argv or "--legacy" in argv
    before = comfy_client.gpu_cache_size_mb(include_legacy=include_legacy)
    res = comfy_client.clear_gpu_cache(include_legacy=include_legacy)
    freed_mb = res["bytes"] / (1024 * 1024)
    scope = "isolated + global" if include_legacy else "isolated (data/gpu_cache)"
    print(f"cleared {scope}: removed {res['files']} files, freed {freed_mb:.1f} MB "
          f"(was {before:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
