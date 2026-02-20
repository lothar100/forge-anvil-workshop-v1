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

# Activate venv
# shellcheck disable=SC1091
source venv/bin/activate

export ZEROCLAW_BUILD_ID="$(date -u +%Y%m%dT%H%M%SZ)_supervisor_autostart"

exec uvicorn app.main:app --host 0.0.0.0 --port 9000 --log-level info
