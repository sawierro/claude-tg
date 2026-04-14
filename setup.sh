#!/bin/bash
cd "$(dirname "$0")"
set -e

echo "========================================"
echo "  Claude-TG: Setup"
echo "========================================"
echo

# Check Python
if ! command -v python3 &> /dev/null; then
    echo "[FAIL] python3 not found. Install Python 3.11+."
    exit 1
fi
echo "[OK] Python found: $(python3 --version)"

# Check Claude CLI
if ! command -v claude &> /dev/null; then
    echo "[WARN] Claude CLI not found. Install: npm install -g @anthropic-ai/claude-code"
else
    echo "[OK] Claude CLI found"
fi

# Create venv if not exists
if [ ! -f .venv/bin/python ]; then
    echo
    echo "Creating virtual environment..."
    python3 -m venv .venv
    echo "[OK] Virtual environment created"
else
    echo "[OK] Virtual environment exists"
fi

# Install dependencies
echo "Installing dependencies..."
.venv/bin/pip install -r requirements.txt --quiet
echo "[OK] Dependencies installed"

# Check .env
if [ -f .env ]; then
    echo "[OK] .env already exists, skipping token setup"
else
    echo
    echo "========================================"
    echo "  Telegram Bot Token"
    echo "========================================"
    echo
    echo "  1. Open Telegram, find @BotFather"
    echo "  2. Send: /newbot"
    echo "  3. Pick any name and username (must end with 'bot')"
    echo "  4. Copy the token"
    echo
    read -p "Paste token here: " TOKEN

    if [ -z "$TOKEN" ]; then
        echo "[FAIL] Token cannot be empty."
        exit 1
    fi

    echo "TELEGRAM_TOKEN=$TOKEN" > .env
    echo "[OK] Token saved"
fi

# Create config.json if not exists
if [ ! -f config.json ]; then
    cp config.example.json config.json
    echo "[OK] config.json created"
else
    echo "[OK] config.json exists"
fi

chmod +x start.sh

echo
echo "========================================"
echo "  DONE! Starting bot..."
echo "========================================"
echo
echo "  First time: send /start to your bot in Telegram"
echo "  to auto-register your account."
echo
echo "  To start later: ./start.sh"
echo

# Start the bot immediately
.venv/bin/python -m bot.main
