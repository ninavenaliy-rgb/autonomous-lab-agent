#!/bin/bash
# Runs the Telegram bot locally on this Mac
# The bot works without Office/Windows — it processes files via python-docx

cd /Users/inga/autonomous-lab-agent

if [ ! -d ".venv" ]; then
  echo "Creating venv..."
  python3 -m venv .venv
  .venv/bin/pip install --upgrade pip
  .venv/bin/pip install -r requirements-deploy.txt
fi

# Load secrets from .env (never committed to git)
if [ -f .env ]; then
  set -a; source .env; set +a
fi

: "${ANTHROPIC_API_KEY:?ANTHROPIC_API_KEY must be set in .env}"
: "${TELEGRAM_BOT_TOKEN:?TELEGRAM_BOT_TOKEN must be set in .env}"

echo "Starting Telegram bot..."
.venv/bin/python telegram_bot/bot.py
