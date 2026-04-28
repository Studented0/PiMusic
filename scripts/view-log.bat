@echo off
:: This script lives in scripts/; cd to the repo root where server.log is.
cd /d "%~dp0\.."
powershell -NoProfile -Command "Get-Content -Path server.log -Wait -Tail 50"
