@echo off
title CS2CaseBot Launcher
echo ================================================
echo   CS2CaseBot - Starting all services
echo ================================================

:: Change to script directory (in case it's run from elsewhere)
cd /d "%~dp0"

:: 1. Start Discord Bot
echo [1/3] Starting Discord bot...
start "CS2CaseBot-Discord" py main.py

:: 2. Start Web Server
echo [2/3] Starting web server on port 8000...
start "CS2CaseBot-Web" py -m uvicorn server:app --host 0.0.0.0 --port 8000

:: 3. Start Cloudflare Tunnel
echo [3/3] Starting Cloudflare tunnel...
if exist ".\cloudflared.exe" (
    start "CS2CaseBot-Tunnel" cmd /k ".\cloudflared.exe tunnel --config config.yml.yaml run"
) else (
    echo cloudflared.exe not found in current directory!
    echo Please make sure cloudflared.exe is in this folder.
    pause
)

echo ================================================
echo All services launched in separate windows.
echo Close each window individually to stop a service.
echo ================================================
pause