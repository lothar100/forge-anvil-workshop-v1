from __future__ import annotations

import json
import concurrent.futures
import os
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .openclaw_langgraph_runtime import run_job_langgraph

BASE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BASE_DIR.parent
DATA_DIR = PROJECT_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = Path(os.getenv("OPENCLAW_LOCAL_DB", str(DATA_DIR / "openclaw.db"))).resolve()
TOKEN_FILE = Path(os.getenv("OPENCLAW_LOCAL_TOKEN_FILE", str(DATA_DIR / "openclaw_token.txt"))).resolve()


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

OPENCLAW_JOB_TIMEOUT_SECONDS = int(os.getenv('OPENCLAW_JOB_TIMEOUT_SECONDS', '300'))


def _get_token() -> str:
    tok = os.getenv("OPENCLAW_AUTH_TOKEN") or os.getenv("OPENCLAW_TOKEN") or ""
    if tok:
        return tok.replace("Bearer ", "").strip()
    if TOKEN_FILE.exists():
        return TOKEN_FILE.read_text(encoding="utf-8").strip()
    # generate token
    tok = uuid.uuid4().hex + uuid.uuid4().hex
    TOKEN_FILE.write_text(tok, encoding="utf-8")
    try:
        os.chmod(TOKEN_FILE, 0o600)
    except Exception:
        pass
    return tok


def _auth_ok(request: Request) -> bool:
    expected = _get_token()
    auth = request.headers.get("authorization") or ""
    auth = auth.strip()
    if auth.lower().startswith("bearer "):
        auth = auth[7:].strip()
    return bool(expected) and auth == expected


def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(str(DB_PATH))
    con.row_factory = sqlite3.Row
    return con


def init_db() -> None:
    con = _connect()
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS jobs (
          job_id TEXT PRIMARY KEY,
          status TEXT NOT NULL,
          created_at TEXT NOT NULL,
          started_at TEXT NULL,
          finished_at TEXT NULL,
          payload TEXT NOT NULL DEFAULT '{}',
          result TEXT NOT NULL DEFAULT '',
          error TEXT NULL,
          logs TEXT NOT NULL DEFAULT '',
          used_model TEXT NULL
        );
        """
    )
    con.commit()
    con.close()


def _append_log(con: sqlite3.Connection, job_id: str, line: str) -> None:
    row = con.execute("SELECT logs FROM jobs WHERE job_id=?", (job_id,)).fetchone()
    prev = row["logs"] if row else ""
    new = (prev + ("\n" if prev else "") + line)[:200000]
    con.execute("UPDATE jobs SET logs=? WHERE job_id=?", (new, job_id))


def _run_job(job_id: str) -> None:
    """LangGraph-based local executor (OpenRouter-backed)."""
    con = _connect()
    con.execute("UPDATE jobs SET status='running', started_at=? WHERE job_id=?", (utcnow_iso(), job_id))
    _append_log(con, job_id, f"[{utcnow_iso()}] started")
    con.commit()

    try:
        row = con.execute("SELECT payload FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        payload = json.loads(row["payload"]) if row and row["payload"] else {}

        _append_log(con, job_id, f"[{utcnow_iso()}] invoking_langgraph")
        con.commit()
        # Guard against jobs that never return (network/LLM hang)
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            fut = ex.submit(run_job_langgraph, payload=payload)
            try:
                r = fut.result(timeout=OPENCLAW_JOB_TIMEOUT_SECONDS)
            except concurrent.futures.TimeoutError:
                fut.cancel()
                r = {'ok': False, 'error': f'timeout_after_{OPENCLAW_JOB_TIMEOUT_SECONDS}s'}
        if not r.get('ok'):
            err = str(r.get('error') or 'unknown_error')
            con.execute(
                "UPDATE jobs SET status='failed', finished_at=?, error=? WHERE job_id=?",
                (utcnow_iso(), err, job_id),
            )
            _append_log(con, job_id, f"[{utcnow_iso()}] failed: {err}")
            con.commit()
            con.close()
            return

        out = str(r.get('output') or '')
        used_model = str(r.get('used_model') or '')
        con.execute(
            "UPDATE jobs SET status='completed', finished_at=?, result=?, used_model=? WHERE job_id=?",
            (utcnow_iso(), out, used_model, job_id),
        )
        _append_log(con, job_id, f"[{utcnow_iso()}] completed")
        con.commit()
        con.close()
        return

    except Exception as e:
        err = f"runner_exception: {e}"
        con.execute(
            "UPDATE jobs SET status='failed', finished_at=?, error=? WHERE job_id=?",
            (utcnow_iso(), err, job_id),
        )
        _append_log(con, job_id, f"[{utcnow_iso()}] failed: {err}")
        con.commit()
        con.close()
        return


app = FastAPI


app = FastAPI(title="OpenClaw (local)", version="0.1.0")


@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse({"ok": True, "service": "openclaw"})



@app.on_event("startup")
def _startup() -> None:
    init_db()
    # Ensure token exists
    _get_token()


@app.post("/jobs")
async def create_job(request: Request) -> JSONResponse:
    if not _auth_ok(request):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)

    payload = await request.json()
    job_id = uuid.uuid4().hex

    con = _connect()
    con.execute(
        "INSERT INTO jobs(job_id, status, created_at, payload) VALUES(?,?,?,?)",
        (job_id, "queued", utcnow_iso(), json.dumps(payload)),
    )
    _append_log(con, job_id, f"[{utcnow_iso()}] queued")
    con.commit()
    con.close()

    t = threading.Thread(target=_run_job, args=(job_id,), daemon=True)
    t.start()

    return JSONResponse({"ok": True, "job_id": job_id})


@app.get("/status/{job_id}")
async def status(job_id: str, request: Request) -> JSONResponse:
    if not _auth_ok(request):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)

    con = _connect()
    row = con.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
    con.close()
    if not row:
        return JSONResponse({"ok": False, "error": "not_found"}, status_code=404)

    out = dict(row)
    payload_obj = {}
    try:
        payload_obj = json.loads(out.get('payload') or '{}') if isinstance(out.get('payload'), str) else (out.get('payload') or {})
    except Exception:
        payload_obj = {}
    agent_model = ''
    try:
        agent_model = str(((payload_obj.get('agent') or {}).get('model') or '')).strip()
    except Exception:
        agent_model = ''
    # Keep payload available but avoid huge responses
    return JSONResponse(
        {
            "ok": True,
            "job_id": out["job_id"],
            "agent_model": agent_model,
            "used_model": (out.get("used_model") or agent_model),
            "status": out["status"],
            "created_at": out["created_at"],
            "started_at": out["started_at"],
            "finished_at": out["finished_at"],
            "result": out["result"],
            "error": out["error"],
            "logs": out["logs"],
        }
    )
