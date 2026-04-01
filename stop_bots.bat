@echo off
title Arbitrage Bot - Stop
cd /d "%~dp0"
echo ===== Stopping Arbitrage Bot =====
set "PYTHON_EXE=%~dp0.venv\Scripts\python.exe"
if exist "%PYTHON_EXE%" (
    "%PYTHON_EXE%" stop_bots.py
) else (
    python stop_bots.py
)
pause
