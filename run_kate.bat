@echo off
title Kate AI Assistant
cd /d "%~dp0"
echo ====================================================
echo           KATE AI COMPANION STARTING
echo ====================================================
echo.
python --version >nul 2>&1
if errorlevel 1 (
    echo Error: Python was not found in PATH!
    echo Please make sure Python 3.10+ is installed.
    pause
    exit /b 1
)

echo Starting Kate Assistant interface and background daemon...
echo.
echo Summon options:
echo   - Ctrl + Space  (or Alt + Space, F9)
echo   - Say "Hey Kate" into microphone
echo   - Click glowing tray icon near Windows clock
echo.
echo Keep this window minimized in the background.
echo ====================================================
echo.

python launch_siri.py
if errorlevel 1 (
    echo.
    echo Kate exited with an error.
    pause
)
