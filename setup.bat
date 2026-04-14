@echo off
cd /d "%~dp0"

echo ========================================
echo   Claude-TG: Setup
echo ========================================
echo.

:: Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [FAIL] Python not found. Install Python 3.11+
    goto :end
)
echo [OK] Python found

:: Create venv
if not exist .venv\Scripts\python.exe (
    echo Creating virtual environment...
    python -m venv .venv
    if errorlevel 1 goto :end
)
echo [OK] Virtual environment ready

:: Install dependencies
echo Installing dependencies...
.venv\Scripts\pip install -r requirements.txt --quiet 2>nul
echo [OK] Dependencies installed

:: Token setup
if exist .env (
    echo [OK] .env exists
    goto :config
)

echo.
echo ========================================
echo   Telegram Bot Token
echo ========================================
echo.
echo   1. Open Telegram, find @BotFather
echo   2. Send: /newbot
echo   3. Pick any name and username
echo   4. Copy the token
echo.
echo   Paste token here and press Enter:
echo.
set /p "TOKEN=> "

if "%TOKEN%"=="" (
    echo [FAIL] Token is empty
    goto :end
)

echo TELEGRAM_TOKEN=%TOKEN%> .env
echo [OK] Token saved

:config
if not exist config.json (
    copy config.example.json config.json >nul
)
echo [OK] config.json ready

echo.
echo ========================================
echo   Starting bot...
echo ========================================
echo   Send /start to your bot in Telegram
echo ========================================
echo.

.venv\Scripts\python -m bot.main

:end
pause
