from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = Path(os.getenv("ZEROCLAW_DB", str(DATA_DIR / "zeroclaw.db"))).resolve()


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(str(DB_PATH))
    con.row_factory = sqlite3.Row
    return con


def _table_exists(con: sqlite3.Connection, name: str) -> bool:
    row = con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
    return bool(row)


def _cols(con: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in con.execute(f"PRAGMA table_info({table})").fetchall()}


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _next_midnight_iso() -> str:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0)
    return midnight.isoformat()


# ---------------------------------------------------------------------------
# Default pipeline seed
# ---------------------------------------------------------------------------
DEFAULT_PIPELINE_BLOCKS = json.dumps([
    {
        "type": "route",
        "config": {
            "label": "Programming Task",
            "condition": "task.type == 'programming'",
        },
    },
    {
        "type": "executor",
        "config": {
            "model": "moonshotai/kimi-m2.5",
            "executor": "OpenRouter",
            "label": "Kimi — First Attempt",
        },
    },
    {
        "type": "review",
        "config": {
            "model": "anthropic/claude-opus-4.6",
            "executor": "OpenRouter",
            "label": "Opus Reviews Output",
            "pass_action": "skip_to_done",
        },
    },
    {
        "type": "retry",
        "config": {
            "model": "moonshotai/kimi-m2.5",
            "executor": "OpenRouter",
            "label": "Kimi — Retry w/ Notes",
            "max_retries": 1,
            "include_review_notes": True,
        },
    },
    {
        "type": "review",
        "config": {
            "model": "anthropic/claude-opus-4.6",
            "executor": "OpenRouter",
            "label": "Opus Reviews Again",
            "pass_action": "skip_to_done",
        },
    },
    {
        "type": "escalate",
        "config": {
            "model": "claude-cli",
            "executor": "Claude CLI",
            "label": "Claude Takes Over",
            "on_limit": "stop",
        },
    },
    {
        "type": "done",
        "config": {"label": "Task Complete"},
    },
])


def migrate_db() -> None:
    """SQLite-safe migrations: add columns if missing (no destructive changes)."""
    con = connect()

    if _table_exists(con, "tasks"):
        have = _cols(con, "tasks")
        add: dict[str, str] = {
            # scheduling
            "schedule_type": "TEXT NOT NULL DEFAULT 'none'",
            "cron_expr": "TEXT NULL",
            "interval_minutes": "INTEGER NULL",
            "is_recurring": "INTEGER NOT NULL DEFAULT 0",
            "next_run_at": "TEXT NULL",
            "last_run_at": "TEXT NULL",
            # execution results
            "last_result": "TEXT NOT NULL DEFAULT ''",
            "last_error": "TEXT NULL",
            "review_summary": "TEXT NULL",
            "retry_count": "INTEGER NOT NULL DEFAULT 0",
            # OpenClaw tracking
            "openclaw_job_id": "TEXT NULL",
            "openclaw_job_status": "TEXT NULL",
            "openclaw_last_status_payload": "TEXT NOT NULL DEFAULT ''",
            # Pipeline resume fields
            "resume_block_index": "INTEGER NULL",
            "resume_pipeline_id": "INTEGER NULL",
        }
        for col, ddl in add.items():
            if col not in have:
                con.execute(f"ALTER TABLE tasks ADD COLUMN {col} {ddl}")

    if _table_exists(con, "decisions"):
        have = _cols(con, "decisions")
        if "updated_at" not in have:
            con.execute("ALTER TABLE decisions ADD COLUMN updated_at TEXT NULL")

    if _table_exists(con, "agents"):
        have = _cols(con, "agents")
        if "pipeline_id" not in have:
            con.execute("ALTER TABLE agents ADD COLUMN pipeline_id INTEGER NULL")

    # action_logs — add layer and model columns if missing
    if _table_exists(con, "action_logs"):
        have = _cols(con, "action_logs")
        if "layer" not in have:
            con.execute("ALTER TABLE action_logs ADD COLUMN layer TEXT NULL")
        if "model" not in have:
            con.execute("ALTER TABLE action_logs ADD COLUMN model TEXT NULL")

    # routines (background automation)
    con.execute("""
        CREATE TABLE IF NOT EXISTS routines (
          id TEXT PRIMARY KEY,
          name TEXT NOT NULL,
          kind TEXT NOT NULL,
          is_enabled INTEGER NOT NULL DEFAULT 1,
          agent_id INTEGER NULL,
          claim_unassigned INTEGER NOT NULL DEFAULT 0,
          description TEXT NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        )
    """)
    if _table_exists(con, "routines"):
        have = _cols(con, "routines")
        if "description" not in have:
            con.execute("ALTER TABLE routines ADD COLUMN description TEXT NULL")

    # agent runtime state (used by routines)
    con.execute("""
        CREATE TABLE IF NOT EXISTS agent_runtime (
          agent_id INTEGER PRIMARY KEY,
          was_running INTEGER NOT NULL DEFAULT 0,
          updated_at TEXT NOT NULL
        )
    """)

    # ── pipelines table ──
    con.execute("""
        CREATE TABLE IF NOT EXISTS pipelines (
          id          INTEGER PRIMARY KEY AUTOINCREMENT,
          name        TEXT NOT NULL,
          description TEXT,
          task_type   TEXT,
          blocks_json TEXT NOT NULL,
          is_active   INTEGER NOT NULL DEFAULT 1,
          created_at  TEXT NOT NULL,
          updated_at  TEXT NOT NULL
        )
    """)

    # ── executor_log table ──
    con.execute("""
        CREATE TABLE IF NOT EXISTS executor_log (
          id               INTEGER PRIMARY KEY AUTOINCREMENT,
          task_id          INTEGER,
          pipeline_id      INTEGER,
          block_index      INTEGER,
          block_type       TEXT,
          model            TEXT,
          executor         TEXT,
          started_at       TEXT,
          duration_seconds REAL,
          success          INTEGER,
          pass_fail        TEXT,
          review_notes     TEXT,
          output_preview   TEXT,
          failure_type     TEXT,
          error            TEXT
        )
    """)

    # ── claude_health table ──
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
    # Seed singleton health row
    row = con.execute("SELECT id FROM claude_health WHERE id=1").fetchone()
    if not row:
        con.execute(
            "INSERT INTO claude_health(id, state, daily_reset_at, updated_at) VALUES(1, 'HEALTHY', ?, ?)",
            (_next_midnight_iso(), _utcnow_iso()),
        )

    # ── routine_state KV table ──
    con.execute("""
        CREATE TABLE IF NOT EXISTS routine_state (
          routine_id TEXT,
          key        TEXT,
          value      TEXT,
          updated_at TEXT,
          PRIMARY KEY(routine_id, key)
        )
    """)

    # ── Seed default pipeline ──
    existing = con.execute("SELECT id FROM pipelines LIMIT 1").fetchone()
    if not existing:
        con.execute(
            "INSERT INTO pipelines(name, description, task_type, blocks_json, is_active, created_at, updated_at) VALUES(?,?,?,?,?,?,?)",
            (
                "Default Pipeline",
                "Kimi first attempt → Opus review → Kimi retry → Opus re-review → Claude CLI escalation",
                "default",
                DEFAULT_PIPELINE_BLOCKS,
                1,
                _utcnow_iso(),
                _utcnow_iso(),
            ),
        )

    con.commit()
    con.close()


def init_db() -> None:
    con = connect()
    cur = con.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS agents (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          name TEXT NOT NULL,
          role TEXT NOT NULL DEFAULT 'general',
          model TEXT NOT NULL DEFAULT 'openai/gpt-5.2',
          pipeline_id INTEGER NULL,
          is_active INTEGER NOT NULL DEFAULT 1,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          title TEXT NOT NULL,
          description TEXT NOT NULL DEFAULT '',
          status TEXT NOT NULL DEFAULT 'pending',
          assigned_agent_id INTEGER NULL,
          due_date TEXT NULL,
          is_critical INTEGER NOT NULL DEFAULT 0,
          requires_approval INTEGER NOT NULL DEFAULT 1,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS decisions (
          decision_id TEXT PRIMARY KEY,
          entity_type TEXT NOT NULL,
          entity_id INTEGER NOT NULL,
          action TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'pending',
          token_hash TEXT NOT NULL,
          token_salt TEXT NOT NULL,
          expires_at TEXT NULL,
          requested_at TEXT NOT NULL,
          decided_at TEXT NULL,
          requester TEXT NOT NULL DEFAULT 'system',
          decider_ip TEXT NULL,
          decider_ua TEXT NULL,
          result_markdown TEXT NOT NULL DEFAULT '',
          error TEXT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS critiques (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          task_id INTEGER NULL,
          title TEXT NOT NULL,
          body TEXT NOT NULL DEFAULT '',
          severity TEXT NOT NULL DEFAULT 'medium',
          created_at TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS action_logs (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          ts TEXT NOT NULL,
          actor TEXT NOT NULL DEFAULT 'system',
          action TEXT NOT NULL,
          entity_type TEXT NOT NULL,
          entity_id TEXT NULL,
          detail TEXT NOT NULL DEFAULT '',
          layer TEXT NULL,
          model TEXT NULL
        )
    """)

    con.commit()
    con.close()

    migrate_db()
