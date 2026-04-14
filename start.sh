#!/bin/bash
cd "$(dirname "$0")"

# Auto-setup if first run
if [ ! -f .venv/bin/python ] || [ ! -f .env ]; then
    echo "First run detected, launching setup..."
    bash setup.sh
    exit
fi

# Check config
[ ! -f config.json ] && cp config.example.json config.json

# Run with all available providers
PROVIDERS=claude,codex .venv/bin/python -m bot.main
