@echo off
cd /d "%~dp0"

:: Auto-setup if first run
if not exist .venv\Scripts\python.exe (
    echo First run detected, launching setup...
    call setup.bat
    exit /b
)

:: Check .env
if not exist .env (
    echo No .env file found, launching setup...
    call setup.bat
    exit /b
)

:: Check config
if not exist config.json (
    copy config.example.json config.json >nul
)

:: Run with all available providers
set PROVIDERS=claude,codex
.venv\Scripts\python -m bot.main
pause
