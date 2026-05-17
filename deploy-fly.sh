#!/bin/bash
set -e

export PATH="/Users/inga/.fly/bin:$PATH"
# Load secrets from .env (never committed to git)
if [ -f .env ]; then
  set -a; source .env; set +a
fi

: "${ANTHROPIC_API_KEY:?ANTHROPIC_API_KEY must be set in .env}"
: "${TELEGRAM_BOT_TOKEN:?TELEGRAM_BOT_TOKEN must be set in .env}"

cd /Users/inga/autonomous-lab-agent

echo "=== Step 1: Login to Fly.io (opens browser) ==="
flyctl auth login

echo "=== Step 2: Create app ==="
flyctl apps create autonomous-lab-agent 2>/dev/null || echo "App name taken, trying with suffix..."
flyctl apps create autonomous-lab-agent-$(date +%s) 2>/dev/null || true

APP_NAME=$(flyctl apps list --json 2>/dev/null | python3 -c "import json,sys; apps=[a['Name'] for a in json.load(sys.stdin) if 'autonomous-lab' in a['Name']]; print(apps[0] if apps else 'autonomous-lab-agent')" 2>/dev/null || echo "autonomous-lab-agent")
sed -i '' "s/^app = .*/app = \"$APP_NAME\"/" fly.toml

echo "=== Step 3: Set secrets ==="
flyctl secrets set \
  ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" \
  TELEGRAM_BOT_TOKEN="$TELEGRAM_BOT_TOKEN" \
  --app "$APP_NAME"

echo "=== Step 4: Deploy ==="
flyctl deploy --app "$APP_NAME" --remote-only

echo ""
echo "✓ Deployed to Fly.io!"
echo "  Logs: flyctl logs --app $APP_NAME"
echo "  Status: flyctl status --app $APP_NAME"
