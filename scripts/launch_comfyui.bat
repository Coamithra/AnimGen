@echo off
rem Detached ComfyUI launcher for AnimGen's local backend.
rem Always passes --disable-dynamic-vram: the default dynamic-VRAM (aimdo) engine stalls
rem 14B renders past Windows' 2s GPU watchdog (TDR) on the 12GB card and kills the server
rem mid-job. comfy_client.preflight() refuses to run a local job without this flag.
rem See ..\Fighter\research\comfyui-gpu-watchdog-crash-and-aimdo.md.
setlocal
if "%ANIMGEN_COMFY_DIR%"=="" (set "COMFY_DIR=%~dp0..\..\comfyui") else (set "COMFY_DIR=%ANIMGEN_COMFY_DIR%")
cd /d "%COMFY_DIR%"
venv\Scripts\python.exe main.py --listen 127.0.0.1 --port 8188 --disable-dynamic-vram %*
