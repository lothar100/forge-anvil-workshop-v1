"""Dual-executor: Claude Code CLI (primary) -> OpenRouter/LangGraph (fallback).

Execution priority:
  1. Claude Code CLI via `claude -p` (if enabled and within daily limit)
  2. OpenClaw/OpenRouter LangGraph runtime (fallback)

If the Claude CLI call fails or times out, the fallback fires automatically.
A daily invocation counter throttles Claude CLI usage.
Every execution logs which executor handled the task.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from .openclaw_langgraph_runtime import run_job_langgraph, _role_system_prompt

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

COUNTER_DB = DATA_DIR / "executor_counters.db"

CLAUDE_CLI_ENABLED = os.getenv("CLAUDE_CLI_ENABLED", "1").lower() in ("1", "true", "yes", "on")
CLAUDE_CLI_PATH = os.getenv("CLAUDE_CLI_PATH", "claude")
CLAUDE_CLI_TIMEOUT = int(os.getenv("CLAUDE_CLI_TIMEOUT_SECONDS", "120"))
CLAUDE_CLI_DAILY_LIMIT = int(os.getenv("CLAUDE_CLI_DAILY_LIMIT", "200"))

# ---------------------------------------------------------------------------
# Daily invocation counter (SQLite-backed)
# ---------------------------------------------------------------------------

_counter_lock = __import__("threading").Lock()


def _counter_connect() -> sqlite3.Connection:
    con = sqlite3.connect(str(COUNTER_DB), timeout=10)
    con.row_factory = sqlite3.Row
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS daily_counts (
            date_iso TEXT NOT NULL,
            executor TEXT NOT NULL,
            count INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (date_iso, executor)
        )
        """
    )
    con.commit()
    return con


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def get_daily_count(executor: str = "claude_cli", today: str | None = None) -> int:
    today = today or date.today().isoformat()
    with _counter_lock:
        con = _counter_connect()
        row = con.execute(
            "SELECT count FROM daily_counts WHERE date_iso=? AND executor=?",
            (today, executor),
        ).fetchone()
        con.close()
    return int(row["count"]) if row else 0


def _increment_count(executor: str, today: str | None = None) -> int:
    today = today or date.today().isoformat()
    with _counter_lock:
        con = _counter_connect()
        row = con.execute(
            "SELECT count FROM daily_counts WHERE date_iso=? AND executor=?",
            (today, executor),
        ).fetchone()
        if row:
            new_count = int(row["count"]) + 1
            con.execute(
                "UPDATE daily_counts SET count=?, updated_at=? WHERE date_iso=? AND executor=?",
                (new_count, _utcnow_iso(), today, executor),
            )
        else:
            new_count = 1
            con.execute(
                "INSERT INTO daily_counts(date_iso, executor, count, updated_at) VALUES(?,?,?,?)",
                (today, executor, 1, _utcnow_iso()),
            )
        con.commit()
        con.close()
    return new_count


# ---------------------------------------------------------------------------
# Claude CLI executor
# ---------------------------------------------------------------------------

def _run_claude_cli(*, payload: dict[str, Any]) -> dict[str, Any]:
    """Run a task via the Claude Code CLI (`claude -p`)."""
    task = (payload.get("task") or {}) if isinstance(payload, dict) else {}
    agent = (payload.get("agent") or {}) if isinstance(payload, dict) else {}
    meta = (payload.get("metadata") or {}) if isinstance(payload, dict) else {}

    title = str(task.get("title") or "(untitled)")
    desc = str(task.get("description") or "")
    role = str(agent.get("role") or meta.get("role") or "")

    system_context = _role_system_prompt(role)
    prompt = (
        f"{system_context}\n\n"
        f"Task Title: {title}\n\n"
        f"Task Description:\n{desc}\n\n"
        "Return your output in markdown. Include a short 'Result' section first."
    )

    try:
        result = subprocess.run(
            [CLAUDE_CLI_PATH, "-p", prompt, "--output-format", "text"],
            capture_output=True,
            text=True,
            timeout=CLAUDE_CLI_TIMEOUT,
            cwd=str(DATA_DIR.parent),
        )
        if result.returncode == 0 and result.stdout.strip():
            return {
                "ok": True,
                "output": result.stdout.strip(),
                "executor": "claude_cli",
                "used_model": "claude-cli-local",
            }
        else:
            stderr = (result.stderr or "").strip()[:500]
            return {
                "ok": False,
                "error": f"claude_cli_exit_{result.returncode}: {stderr}",
                "executor": "claude_cli",
            }
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "error": f"claude_cli_timeout_{CLAUDE_CLI_TIMEOUT}s",
            "executor": "claude_cli",
        }
    except FileNotFoundError:
        return {
            "ok": False,
            "error": "claude_cli_not_found",
            "executor": "claude_cli",
        }
    except Exception as e:
        return {
            "ok": False,
            "error": f"claude_cli_error: {e}",
            "executor": "claude_cli",
        }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def execute_task(*, payload: dict[str, Any]) -> dict[str, Any]:
    """Execute a task with Claude CLI (primary) and OpenRouter/LangGraph (fallback).

    Returns: {ok, output, executor, used_model, fallback_reason?}
    """
    today = date.today().isoformat()
    fallback_reason: str | None = None

    # --- Try Claude CLI first ---
    if CLAUDE_CLI_ENABLED:
        count = get_daily_count("claude_cli", today)
        if count < CLAUDE_CLI_DAILY_LIMIT:
            result = _run_claude_cli(payload=payload)
            _increment_count("claude_cli", today)
            if result.get("ok"):
                return result
            # Failed — record reason and fall through
            fallback_reason = result.get("error", "unknown_cli_error")
        else:
            fallback_reason = f"daily_limit_reached ({count}/{CLAUDE_CLI_DAILY_LIMIT})"
    else:
        fallback_reason = "claude_cli_disabled"

    # --- Fallback: OpenRouter / LangGraph ---
    result = run_job_langgraph(payload=payload)
    result["executor"] = "openrouter_langgraph"
    result["fallback_reason"] = fallback_reason
    _increment_count("openrouter_langgraph", today)
    return result
