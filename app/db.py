from __future__ import annotations

import os
import sqlite3
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


def migrate_db() -> None:
    """SQLite-safe migrations: add columns if missing (no destructive changes)."""
    con = connect()

    if _table_exists(con, "tasks"):
        have = _cols(con, "tasks")
        add: dict[str, str] = {
            # scheduling
            "schedule_type": "TEXT NOT NULL DEFAULT 'none'",  # none|interval|cron
            "cron_expr": "TEXT NULL",
            "interval_minutes": "INTEGER NULL",
            "is_recurring": "INTEGER NOT NULL DEFAULT 0",
            "next_run_at": "TEXT NULL",
            "last_run_at": "TEXT NULL",
            # execution results
            "last_result": "TEXT NOT NULL DEFAULT ''",
            "last_error": "TEXT NULL",
            # OpenClaw tracking
            "openclaw_job_id": "TEXT NULL",
            "openclaw_job_status": "TEXT NULL",
            "openclaw_last_status_payload": "TEXT NOT NULL DEFAULT ''",
        }
        for col, ddl in add.items():
            if col not in have:
                con.execute(f"ALTER TABLE tasks ADD COLUMN {col} {ddl}")

    if _table_exists(con, "decisions"):
        have = _cols(con, "decisions")
        if "updated_at" not in have:
            con.execute("ALTER TABLE decisions ADD COLUMN updated_at TEXT NULL")

    # routines (background automation)
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS routines (
          id TEXT PRIMARY KEY,
          name TEXT NOT NULL,
          kind TEXT NOT NULL,
          is_enabled INTEGER NOT NULL DEFAULT 1,
          agent_id INTEGER NULL,
          claim_unassigned INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        """
    )

    # agent runtime state (used by routines)
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_runtime (
          agent_id INTEGER PRIMARY KEY,
          was_running INTEGER NOT NULL DEFAULT 0,
          updated_at TEXT NOT NULL
        );
        """
    )

    con.commit()
    con.close()


def init_db() -> None:
    con = connect()
    cur = con.cursor()

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS agents (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          name TEXT NOT NULL,
          role TEXT NOT NULL DEFAULT 'general',
          model TEXT NOT NULL DEFAULT 'openai/gpt-5.2',
          is_active INTEGER NOT NULL DEFAULT 1,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        """
    )

    cur.execute(
        """
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
        );
        """
    )

    cur.execute(
        """
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
        );
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS critiques (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          task_id INTEGER NULL,
          title TEXT NOT NULL,
          body TEXT NOT NULL DEFAULT '',
          severity TEXT NOT NULL DEFAULT 'medium',
          created_at TEXT NOT NULL
        );
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS action_logs (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          ts TEXT NOT NULL,
          actor TEXT NOT NULL DEFAULT 'system',
          action TEXT NOT NULL,
          entity_type TEXT NOT NULL,
          entity_id TEXT NULL,
          detail TEXT NOT NULL DEFAULT ''
        );
        """
    )

    con.commit()
    con.close()

    migrate_db()
