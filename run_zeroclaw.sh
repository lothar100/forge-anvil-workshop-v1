#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

PORT="${PORT:-9000}"
HOST="${HOST:-0.0.0.0}"

# Load .env if present
if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

# Ensure Claude CLI is installed
if ! command -v claude &>/dev/null; then
  echo "[run_zeroclaw] Installing Claude CLI..." >&2
  if command -v npm &>/dev/null; then
    npm install -g @anthropic-ai/claude-code 2>/dev/null || true
  else
    echo "[run_zeroclaw] WARNING: npm not found, Claude CLI not installed" >&2
  fi
fi
echo "[run_zeroclaw] Claude CLI: $(claude --version 2>/dev/null || echo 'not available')" >&2

# Expectations (Option 4)
export ZEROCLAW_EXPECT_CWD="${ZEROCLAW_EXPECT_CWD:-$ROOT}"
export ZEROCLAW_EXPECT_DB="${ZEROCLAW_EXPECT_DB:-$ROOT/data/zeroclaw.db}"

# Build id
export ZEROCLAW_BUILD_ID="${ZEROCLAW_BUILD_ID:-"$(date -u +%Y%m%dT%H%M%SZ)_run"}"

# venv
if [ ! -d venv ]; then
  python3 -m venv venv
fi
# shellcheck disable=SC1091
source venv/bin/activate

pip install -r requirements.txt >/dev/null

mkdir -p data

start_uvicorn() {
  echo "[run_zeroclaw] starting uvicorn on ${HOST}:${PORT} build=${ZEROCLAW_BUILD_ID}" >&2
  nohup uvicorn app.main:app --host "$HOST" --port "$PORT" --log-level info >> data/uvicorn.log 2>&1 &
  echo $!
}

kill_port() {
  local pids
  pids=$(ss -ltnp "sport = :$PORT" 2>/dev/null | awk -F'pid=' 'NR>1{split($2,a,","); print a[1]}' | sort -u | tr '\n' ' ')
  if [ -n "${pids// }" ]; then
    echo "[run_zeroclaw] killing pids on :$PORT => $pids" >&2
    # shellcheck disable=SC2086
    kill -9 $pids || true
  fi
  # fallback
  pkill -f "uvicorn .*--port[= ]$PORT" >/dev/null 2>&1 || true
}

verify() {
  local cnt hdr body
  cnt=$(ss -ltnp "sport = :$PORT" 2>/dev/null | grep -c LISTEN || true)
  if [ "$cnt" != "1" ]; then
    echo "[run_zeroclaw] VERIFY FAIL: expected 1 LISTEN on :$PORT, got $cnt" >&2
    ss -ltnp | grep ":$PORT" || true
    exit 1
  fi
  hdr=$(curl -is "http://localhost:$PORT/version" | tr -d '\r')
  echo "$hdr" | grep -qi "x-zeroclaw-build: ${ZEROCLAW_BUILD_ID}" || {
    echo "[run_zeroclaw] VERIFY FAIL: missing/incorrect X-ZeroClaw-Build header" >&2
    echo "$hdr" >&2
    exit 1
  }
  body=$(echo "$hdr" | awk 'BEGIN{s=0} /^\{/ {s=1} {if(s) print}')
  echo "$body" | grep -q "\"build_id\":\"${ZEROCLAW_BUILD_ID}\"" || {
    echo "[run_zeroclaw] VERIFY FAIL: JSON build_id mismatch" >&2
    echo "$body" >&2
    exit 1
  }
  echo "[run_zeroclaw] OK: live on :$PORT build=${ZEROCLAW_BUILD_ID}" >&2
}

# Double-tap restart
_pid1=$(start_uvicorn)
sleep 0.8
kill_port
sleep 0.5
_pid2=$(start_uvicorn)

# Give it a moment
sleep 1.0
verify

echo "build=${ZEROCLAW_BUILD_ID} pid=${_pid2} port=${PORT} root=${ROOT}"
