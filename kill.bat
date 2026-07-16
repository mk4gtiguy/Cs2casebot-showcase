@echo off
title CS2CaseBot Stopper
echo ================================================
echo   CS2CaseBot - Stopping all services
echo ================================================

:: Kill processes by window title (more reliable)
taskkill /FI "WINDOWTITLE eq CS2CaseBot-Web" /F >nul 2>&1
taskkill /FI "WINDOWTITLE eq CS2CaseBot-Discord" /F >nul 2>&1
taskkill /FI "WINDOWTITLE eq CS2CaseBot-Tunnel" /F >nul 2>&1

:: Fallback: kill by process name in case windows closed
taskkill /IM uvicorn.exe /F >nul 2>&1
taskkill /IM python.exe /F >nul 2>&1   :: Might kill other Python processes - use with caution
taskkill /IM cloudflared.exe /F >nul 2>&1

echo All services stopped.
pause