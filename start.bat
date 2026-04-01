@echo off
title Arbitrage Bot - Start
cd /d "%~dp0"
echo ===== Starting Arbitrage Bot =====
set "PYTHON_EXE=%~dp0.venv\Scripts\python.exe"
if exist "%PYTHON_EXE%" (
    "%PYTHON_EXE%" -m src.launcher
) else (
    python -m src.launcher
)
pause
