@echo off
title Arbitrage Bot - Manual Roundtrip Test
cd /d "%~dp0"
set "PYTHON_EXE=%~dp0.venv\Scripts\python.exe"
if exist "%PYTHON_EXE%" (
    "%PYTHON_EXE%" -m src.tools.manual_roundtrip_test %*
) else (
    python -m src.tools.manual_roundtrip_test %*
)
pause
