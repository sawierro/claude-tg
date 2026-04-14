#!/bin/bash
cd "$(dirname "$0")"

if [ ! -f .venv/bin/python ] || [ ! -f .env ]; then
    echo "First run detected, launching setup..."
    bash setup.sh
    exit
fi

[ ! -f config.json ] && cp config.example.json config.json

PROVIDERS=codex .venv/bin/python -m bot.main
