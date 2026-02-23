#!/usr/bin/env bash
set -euo pipefail
cd /a0/usr/workdir/zeroclaw

# Load .env if present
if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

# Ensure Claude CLI is installed
if ! command -v claude &>/dev/null; then
  echo "[supervisor] Installing Claude CLI..." >&2
  if command -v npm &>/dev/null; then
    npm install -g @anthropic-ai/claude-code 2>/dev/null || true
  else
    echo "[supervisor] WARNING: npm not found, Claude CLI not installed" >&2
  fi
fi
echo "[supervisor] Claude CLI: $(claude --version 2>/dev/null || echo 'not available')" >&2

# Activate venv
# shellcheck disable=SC1091
source venv/bin/activate

export ZEROCLAW_BUILD_ID="$(date -u +%Y%m%dT%H%M%SZ)_supervisor_autostart"

exec uvicorn app.main:app --host 0.0.0.0 --port 9000 --log-level info
