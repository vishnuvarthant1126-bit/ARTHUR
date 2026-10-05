@echo off
rem Start ARTHUR: double-click this file. Close the window (or press Ctrl+C) to stop it.
title ARTHUR
cd /d "%~dp0"

rem Already running? Then just open it.
powershell -NoProfile -Command "try { Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8000/health -TimeoutSec 3 | Out-Null; exit 0 } catch { exit 1 }"
if %errorlevel%==0 (
    echo ARTHUR is already running: http://127.0.0.1:8000
    timeout /t 5 >nul
    exit /b
)

echo Starting ARTHUR... keep this window open (you can minimise it).
echo On this PC: http://127.0.0.1:8000
".venv\Scripts\python.exe" -m uvicorn app.main:app
pause
