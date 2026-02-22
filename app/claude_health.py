"""Claude CLI Health State Machine.

Tracks the operational state of the Claude CLI executor across invocations.
State is persisted in the ``claude_health`` table (singleton row, id=1).

States
------
HEALTHY          — CLI is working normally (default).
DEGRADED         — Intermittent rate limits; still usable.
AUTH_FAILED      — Auth token expired / invalid; needs human intervention.
DAILY_LIMIT_HIT  — Daily usage exhausted; wait for midnight reset.
UNAVAILABLE      — Too many errors; back off.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone, timedelta

from .db import connect

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
HEALTHY = "HEALTHY"
DEGRADED = "DEGRADED"
AUTH_FAILED = "AUTH_FAILED"
DAILY_LIMIT_HIT = "DAILY_LIMIT_HIT"
UNAVAILABLE = "UNAVAILABLE"

_CONSECUTIVE_THRESHOLD = 5  # errors/timeouts before UNAVAILABLE
_FAILURE_WINDOW_MINUTES = 30

# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _utcnow_iso() -> str:
    return _utcnow().isoformat()


def _next_midnight() -> str:
    now = _utcnow()
    midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0)
    return midnight.isoformat()


def ensure_table() -> None:
    """Create the claude_health table if it doesn't exist."""
    con = connect()
    con.execute("""
        CREATE TABLE IF NOT EXISTS claude_health (
            id                   INTEGER PRIMARY KEY CHECK(id = 1),
            state                TEXT    NOT NULL DEFAULT 'HEALTHY',
            last_success         TEXT,
            last_failure         TEXT,
            last_failure_type    TEXT,
            consecutive_failures INTEGER NOT NULL DEFAULT 0,
            daily_invocations    INTEGER NOT NULL DEFAULT 0,
            daily_reset_at       TEXT,
            updated_at           TEXT
        )
    """)
    # Seed singleton row if missing
    row = con.execute("SELECT id FROM claude_health WHERE id=1").fetchone()
    if not row:
        con.execute(
            "INSERT INTO claude_health(id, state, daily_reset_at, updated_at) VALUES(1, 'HEALTHY', ?, ?)",
            (_next_midnight(), _utcnow_iso()),
        )
    con.commit()
    con.close()


def _get_row() -> dict:
    ensure_table()
    con = connect()
    row = con.execute("SELECT * FROM claude_health WHERE id=1").fetchone()
    con.close()
    return dict(row) if row else {}


def _update(**fields: str | int | None) -> None:
    ensure_table()
    sets = ", ".join(f"{k}=?" for k in fields)
    vals = list(fields.values())
    con = connect()
    con.execute(f"UPDATE claude_health SET {sets}, updated_at=? WHERE id=1", vals + [_utcnow_iso()])
    con.commit()
    con.close()


# ---------------------------------------------------------------------------
# Daily reset check
# ---------------------------------------------------------------------------

def _maybe_reset_daily(row: dict) -> bool:
    """Reset daily counters if past midnight. Returns True if reset happened."""
    reset_at = row.get("daily_reset_at")
    if not reset_at:
        return False
    try:
        dt = datetime.fromisoformat(reset_at)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except Exception:
        return False
    if _utcnow() >= dt:
        _update(
            state=HEALTHY,
            daily_invocations=0,
            daily_reset_at=_next_midnight(),
            consecutive_failures=0,
        )
        return True
    return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_state() -> str:
    """Return the current health state, applying daily reset if needed."""
    row = _get_row()
    if not row:
        return HEALTHY
    # Auto-reset daily limit at midnight
    if row.get("state") == DAILY_LIMIT_HIT:
        if _maybe_reset_daily(row):
            return HEALTHY
    # Auto-recover from UNAVAILABLE after cooldown
    if row.get("state") == UNAVAILABLE:
        cooldown_min = int(os.getenv("CLAUDE_UNAVAILABLE_COOLDOWN_MINUTES", "30"))
        last_fail = row.get("last_failure")
        if last_fail:
            try:
                dt = datetime.fromisoformat(last_fail)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                if _utcnow() - dt >= timedelta(minutes=cooldown_min):
                    _update(state=HEALTHY, consecutive_failures=0)
                    return HEALTHY
            except Exception:
                pass
    return row.get("state", HEALTHY)


def get_full_status() -> dict:
    """Return the full health row for dashboard display."""
    row = _get_row()
    if not row:
        return {"state": HEALTHY}
    # Apply auto-resets
    get_state()
    return _get_row()


def record_success() -> None:
    """Record a successful Claude CLI invocation."""
    row = _get_row()
    invocations = int(row.get("daily_invocations") or 0) + 1
    new_state = HEALTHY
    _update(
        state=new_state,
        last_success=_utcnow_iso(),
        consecutive_failures=0,
        daily_invocations=invocations,
    )


def record_failure(failure_type: str) -> None:
    """Record a failed Claude CLI invocation and transition state."""
    from .claude_executor import (
        CLAUDE_FAIL_AUTH,
        CLAUDE_FAIL_RATE_LIMIT,
        CLAUDE_FAIL_DAILY_LIMIT,
        CLAUDE_FAIL_TIMEOUT,
        CLAUDE_FAIL_ERROR,
    )

    row = _get_row()
    consecutive = int(row.get("consecutive_failures") or 0) + 1
    invocations = int(row.get("daily_invocations") or 0) + 1
    current = row.get("state", HEALTHY)

    # Determine new state based on failure type
    if failure_type == CLAUDE_FAIL_AUTH:
        new_state = AUTH_FAILED
    elif failure_type == CLAUDE_FAIL_DAILY_LIMIT:
        new_state = DAILY_LIMIT_HIT
    elif failure_type == CLAUDE_FAIL_RATE_LIMIT:
        if current == HEALTHY:
            new_state = DEGRADED
        elif current == DEGRADED:
            new_state = DEGRADED  # stay degraded until daily or consecutive threshold
        else:
            new_state = current
    elif failure_type in (CLAUDE_FAIL_TIMEOUT, CLAUDE_FAIL_ERROR):
        if consecutive >= _CONSECUTIVE_THRESHOLD:
            new_state = UNAVAILABLE
        else:
            new_state = current
    else:
        new_state = current

    _update(
        state=new_state,
        last_failure=_utcnow_iso(),
        last_failure_type=failure_type,
        consecutive_failures=consecutive,
        daily_invocations=invocations,
    )


def manual_reset() -> None:
    """Manual reset from dashboard (e.g. after running ``claude login``)."""
    _update(
        state=HEALTHY,
        consecutive_failures=0,
        last_failure_type=None,
    )
