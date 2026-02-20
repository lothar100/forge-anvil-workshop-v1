#!/usr/bin/env bash
set -euo pipefail
cd /a0/usr/workdir/zeroclaw

# venv bootstrap (idempotent)
if [ ! -d venv ]; then
  python3 -m venv venv
fi
# shellcheck disable=SC1091
source venv/bin/activate

python -m pip install -U pip >/dev/null
python -m pip install -r requirements.txt >/dev/null

export ZEROCLAW_BUILD_ID="${ZEROCLAW_BUILD_ID:-$(date -u +%Y%m%dT%H%M%SZ)_autostart}"

# Run in foreground so Docker can restart it if it crashes
exec uvicorn app.main:app --host 0.0.0.0 --port 9000 --log-level info
