@echo off
cd /d "%~dp0"
powershell -NoProfile -Command "Get-Content -Path server.log -Wait -Tail 50"
