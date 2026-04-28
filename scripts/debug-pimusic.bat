@echo off
:: This script lives in scripts/; cd to the repo root.
cd /d "%~dp0\.."
echo Stopping background PiMusic task...
schtasks /End /TN "PiMusic Server" >nul 2>&1
taskkill /F /IM python.exe >nul 2>&1
echo.
echo Starting server in visible mode. Close this window to stop.
echo.
python -u server\spotify_server.py
pause
