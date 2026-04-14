@echo off
cd /d "%~dp0"

if not exist .venv\Scripts\python.exe (
    echo First run detected, launching setup...
    call setup.bat
    exit /b
)

if not exist .env (
    echo No .env file found, launching setup...
    call setup.bat
    exit /b
)

if not exist config.json (
    copy config.example.json config.json >nul
)

set PROVIDERS=claude
.venv\Scripts\python -m bot.main
pause
