@echo off
rem Detached ComfyUI launcher for AnimGen's local backend.
rem Mirrors comfy_client.REQUIRED_FLAGS: --disable-dynamic-vram AND --disable-async-offload
rem turn off BOTH mid-kernel PCIe weight-streaming paths (aimdo dynamic VRAM + async weight
rem offloading, the second one default-on on Nvidia/AMD), either of which can stall a 14B op
rem past Windows' 2s GPU watchdog (TDR) on the 12GB card and kill the server mid-job;
rem --cache-none avoids leaving a prior run's weights pinned in VRAM. comfy_client.preflight()
rem refuses to run a local job against a server still streaming weights.
rem See ..\Fighter\research\comfyui-gpu-watchdog-crash-and-aimdo.md.
setlocal
if "%ANIMGEN_COMFY_DIR%"=="" (set "COMFY_DIR=%~dp0..\..\comfyui") else (set "COMFY_DIR=%ANIMGEN_COMFY_DIR%")
cd /d "%COMFY_DIR%"
venv\Scripts\python.exe main.py --listen 127.0.0.1 --port 8188 --disable-dynamic-vram --disable-async-offload --cache-none %*
