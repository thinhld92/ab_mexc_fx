@echo off
title Arbitrage Bot - Setup Venv
cd /d "%~dp0"
echo ===== Setting up Python virtual environment =====
powershell -ExecutionPolicy Bypass -File "%~dp0setup_venv.ps1"
pause
