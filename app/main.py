from __future__ import annotations

import json
import os
import re
import subprocess
import socket
import secrets
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import httpx
import markdown2
from croniter import croniter
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .db import connect, init_db, migrate_db
from .emailer import send_email
from .approvals import create_decision, verify_decision_token, apply_decision
from .openclaw import dispatch_job, get_status, normalize_state
from .routines import router as routines_router, tick_routines
from .agent_files import ensure_agent_dir, list_agent_files, read_agent_file, write_agent_file, delete_agent_file, rename_agent_dir
from . import claude_health
from .pipeline_executor import run_pipeline, resume_pipeline

BASE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BASE_DIR.parent

load_dotenv(PROJECT_DIR / ".env")

PUBLIC_BASE_URL = (os.getenv("PUBLIC_BASE_URL") or "http://localhost:9000").rstrip("/")
APPROVER_EMAIL = os.getenv("APPROVER_EMAIL") or os.getenv("EMAIL_TO") or os.getenv("MAIL_TO") or os.getenv("SMTP_USER") or ""
APPROVAL_UPSTREAM_BASE_URL = (os.getenv("APPROVAL_UPSTREAM_BASE_URL") or "").rstrip("/")

SCHEDULER_TICK_SECONDS = int(os.getenv("SCHEDULER_TICK_SECONDS", "20"))
OPENCLAW_POLL_SECONDS = int(os.getenv("OPENCLAW_POLL_SECONDS", "20"))
SCHEDULE_APPROVAL_LEAD_SECONDS = int(os.getenv("SCHEDULE_APPROVAL_LEAD_SECONDS", "300"))
OPENCLAW_ENABLED = (os.getenv("OPENCLAW_ENABLED", "1").lower() in ("1","true","yes","on"))
CLAUDE_CLI_ENABLED = (os.getenv("CLAUDE_CLI_ENABLED", "1").lower() in ("1","true","yes","on"))

DASHBOARD_APPROVALS_ENABLED = (os.getenv("DASHBOARD_APPROVALS_ENABLED", "0").lower() in ("1", "true", "yes", "on"))
AUTO_CRITICAL_KEYWORDS = [k.strip().lower() for k in os.getenv("AUTO_CRITICAL_KEYWORDS", "security,auth,login,payment,deploy,release,prod,approval").split(",") if k.strip()]

def _seems_critical(title: str, description: str) -> bool:
    t = (title or "").lower()
    d = (description or "").lower()
    blob = f"{t} {d}"
    return any(k in blob for k in AUTO_CRITICAL_KEYWORDS)


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _env_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None or v == '':
        return default
    return str(v).strip().lower() in ('1','true','yes','on')

def _tcp_listening(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.2):
            return True
    except Exception:
        return False


def _ensure_local_openclaw() -> None:
    enabled = os.getenv('OPENCLAW_LOCAL_ENABLED', '1').lower() in ('1','true','yes','on')
    if not enabled:
        return

    if not (os.getenv('OPENCLAW_BASE_URL') or '').strip():
        os.environ['OPENCLAW_BASE_URL'] = 'http://localhost:9100'

    token_file = PROJECT_DIR / 'data' / 'openclaw_token.txt'
    if not (os.getenv('OPENCLAW_AUTH_TOKEN') or os.getenv('OPENCLAW_TOKEN') or '').strip():
        if token_file.exists():
            tok = token_file.read_text(encoding='utf-8').strip()
        else:
            tok = secrets.token_urlsafe(32)
            token_file.write_text(tok, encoding='utf-8')
            try:
                os.chmod(token_file, 0o600)
            except Exception:
                pass
        os.environ['OPENCLAW_AUTH_TOKEN'] = tok

    if _tcp_listening('127.0.0.1', 9100):
        return

    import sys
    log_path = PROJECT_DIR / 'data' / 'openclaw.log'
    cmd = [sys.executable,'-m','uvicorn','app.openclaw_local:app','--host','0.0.0.0','--port','9100']
    subprocess.Popen(cmd, cwd=str(PROJECT_DIR), stdout=open(log_path, 'ab'), stderr=subprocess.STDOUT)
    _log('openclaw_local_started', 'system', None, f"base_url={os.environ.get('OPENCLAW_BASE_URL')}")



app = FastAPI(title="ZeroClaw", version="0.3.0")

BUILD_ID = "20260222T000000Z_pipeline_v2"


@app.middleware("http")
async def _stamp_build_header(request, call_next):
    resp = await call_next(request)
    resp.headers["X-ZeroClaw-Build"] = BUILD_ID
    return resp

app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
templates.env.globals['build_id'] = BUILD_ID

# Register Jinja2 filter for JSON parsing in templates
def _from_json(value):
    try:
        return json.loads(value) if isinstance(value, str) else value
    except Exception:
        return []
templates.env.filters['from_json'] = _from_json

app.include_router(routines_router)

_scheduler: Optional[BackgroundScheduler] = None


@app.on_event("startup")
def startup() -> None:
    init_db()
    migrate_db()
    _ensure_local_openclaw()
    _maybe_import_backlog()
    _maybe_seed_agents()
    _start_scheduler()


def _log(action: str, entity_type: str='system', entity_id: str | None=None, detail: str='', *, layer: str='zeroclaw', model: str | None=None) -> None:
    con = connect()
    con.execute(
        "INSERT INTO action_logs(ts, action, entity_type, entity_id, detail, layer, model) VALUES(?,?,?,?,?,?,?)",
        (utcnow_iso(), action, entity_type, entity_id, detail, layer, model),
    )
    con.commit()
    con.close()


def _maybe_import_backlog() -> None:
    backlog = Path("/a0/usr/uploads/backlog.md")
    if not backlog.exists():
        return

    con = connect()
    any_task = con.execute("SELECT id FROM tasks LIMIT 1").fetchone()
    if any_task:
        con.close()
        return

    text = backlog.read_text(encoding="utf-8", errors="replace")
    titles: list[str] = []
    for ln in text.splitlines():
        if ln.startswith("## "):
            t = ln[3:].strip()
            if t:
                titles.append(t)

    if not titles and text.strip():
        titles = ["Imported Backlog"]

    for title in titles:
        con.execute(
            """
            INSERT INTO tasks(title, description, status, assigned_agent_id, due_date, is_critical, requires_approval, created_at, updated_at)
            VALUES(?, ?, 'pending', NULL, NULL, ?, ?, ?, ?)
            """,
            (title, "Imported from backlog.md", (1 if _seems_critical(title, "Imported from backlog.md") else 0), 1, utcnow_iso(), utcnow_iso()),
        )

    con.execute(
        "INSERT INTO action_logs(ts, action, entity_type, entity_id, detail, layer, model) VALUES(?,?,?,?,?,?,?)",
        (utcnow_iso(), "backlog_import", "system", None, f"Imported {len(titles)} tasks"),
    )
    con.commit()
    con.close()


def _maybe_seed_agents() -> None:
    con = connect()
    any_agent = con.execute("SELECT id FROM agents LIMIT 1").fetchone()
    if any_agent:
        con.close()
        return

    seed = [
        ("Programmer", "programming"),
        ("Architect", "architecture"),
        ("Reviewer", "reviewing"),
        ("Reporter", "reporting"),
    ]
    for name, role in seed:
        con.execute(
            "INSERT INTO agents(name, role, model, is_active, created_at, updated_at) VALUES(?,?,?,?,?,?)",
            (name, role, "openai/gpt-5.2", 1, utcnow_iso(), utcnow_iso()),
        )
        # Create agent internal files
        ensure_agent_dir(name, role)

    con.execute(
        "INSERT INTO action_logs(ts, action, entity_type, entity_id, detail, layer, model) VALUES(?,?,?,?,?,?,?)",
        (utcnow_iso(), "agent_seed", "system", None, f"Seeded {len(seed)} agents", "anvil", None),
    )
    con.commit()
    con.close()


def _start_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        return

    _scheduler = BackgroundScheduler(daemon=True)
    _scheduler.add_job(_tick_scheduled_tasks_safe, 'interval', seconds=SCHEDULER_TICK_SECONDS)
    _scheduler.add_job(_poll_openclaw_safe, 'interval', seconds=OPENCLAW_POLL_SECONDS)
    _scheduler.add_job(_tick_routines_safe, 'interval', seconds=int(os.getenv('ROUTINES_TICK_SECONDS','10')))
    _scheduler.add_job(_tick_resume_paused_safe, 'interval', seconds=30)

    minutes = int(os.getenv('SUMMARY_EMAIL_EVERY_MINUTES', '360'))
    if minutes > 0:
        _scheduler.add_job(_send_summary_email_safe, 'interval', minutes=minutes)

    _scheduler.start()



def _send_summary_email_safe() -> None:
    try:
        _send_summary_email()
    except Exception as e:
        _log("summary_email_error", "system", None, str(e))



def _tick_scheduled_tasks_safe() -> None:
    try:
        _tick_scheduled_tasks()
    except Exception as e:
        _log('scheduler_tick_error', 'system', None, str(e))


def _poll_openclaw_safe() -> None:
    try:
        _poll_openclaw_jobs()
    except Exception as e:
        _log('openclaw_poll_error', 'system', None, str(e))



def _tick_routines_safe() -> None:
    try:
        tick_routines()
    except Exception as e:
        _log('routines_tick_error', 'system', None, str(e))


def _tick_resume_paused_safe() -> None:
    """Resume paused/queued tasks when Claude CLI recovers."""
    try:
        _tick_resume_paused()
    except Exception as e:
        _log('resume_paused_error', 'system', None, str(e))


def _tick_resume_paused() -> None:
    """Check if Claude CLI is healthy and resume paused/queued tasks."""
    state = claude_health.get_state()
    if state != claude_health.HEALTHY:
        return

    con = connect()
    paused = con.execute("SELECT id FROM tasks WHERE status IN ('paused_limit', 'queued_for_claude')").fetchall()
    con.close()

    for row in paused:
        task_id = row['id']
        try:
            _log('pipeline_resume', 'task', str(task_id), 'claude_cli recovered, resuming pipeline')
            # Run in a thread to avoid blocking the scheduler
            t = threading.Thread(target=resume_pipeline, args=(task_id,), daemon=True)
            t.start()
        except Exception as e:
            _log('pipeline_resume_error', 'task', str(task_id), str(e))


def _send_summary_email() -> None:
    if not APPROVER_EMAIL:
        return

    con = connect()
    tasks = con.execute("SELECT id,title,status,due_date,updated_at FROM tasks ORDER BY updated_at DESC LIMIT 50").fetchall()
    pending = con.execute("SELECT decision_id,entity_type,entity_id,action,status FROM decisions WHERE status='pending' ORDER BY requested_at DESC").fetchall()
    con.close()

    rows = "".join(f"<tr><td>{t['id']}</td><td>{t['title']}</td><td>{t['status']}</td><td>{t['due_date'] or ''}</td></tr>" for t in tasks)
    decs = "".join(f"<li>{d['decision_id']} - {d['entity_type']}:{d['entity_id']} - {d['action']} - {d['status']}</li>" for d in pending)

    html = f"""
    <h2>ZeroClaw Summary</h2>
    <p>Dashboard: <a href=\"{PUBLIC_BASE_URL}/dashboard\">{PUBLIC_BASE_URL}/dashboard</a></p>
    <h3>Tasks (latest 50)</h3>
    <table border=\"1\" cellpadding=\"6\" cellspacing=\"0\">
      <tr><th>ID</th><th>Title</th><th>Status</th><th>Due</th></tr>
      {rows}
    </table>
    <h3>Pending decisions</h3>
    <ul>{decs or '<li>None</li>'}</ul>
    """

    send_email(to_addr=APPROVER_EMAIL, subject="[ZeroClaw] Summary report", html_body=html)
    _log("summary_email_sent", "system", None, f"to={APPROVER_EMAIL}")


@app.get("/", response_class=HTMLResponse)
def root() -> RedirectResponse:
    return RedirectResponse(url="/dashboard", status_code=302)


@app.get("/version")
def version() -> dict:
    pid = os.getpid()
    cwd = os.getcwd()
    return {"ok": True, "build_id": BUILD_ID, "pid": pid, "cwd": cwd}


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request) -> HTMLResponse:
    con = connect()
    tasks = con.execute("SELECT * FROM tasks ORDER BY updated_at DESC").fetchall()
    agents = con.execute("SELECT * FROM agents WHERE is_active=1 ORDER BY name").fetchall()
    con.close()
    cols = ["pending", "approved", "rejected", "active", "blocked", "paused_limit", "queued_for_claude", "dev_done", "review", "done"]
    tasks_by_status = {c: [] for c in cols}
    paused_count = 0
    queued_count = 0
    for t in tasks:
        st = t["status"]
        if st == 'running':
            st = 'active'
        if st == 'completed':
            st = 'dev_done'
        if st == 'paused_limit':
            paused_count += 1
        if st == 'queued_for_claude':
            queued_count += 1
        if st not in tasks_by_status:
            st = "pending"
        tasks_by_status[st].append(t)

    # Remove empty paused/queued columns
    if not tasks_by_status['paused_limit']:
        cols = [c for c in cols if c != 'paused_limit']
    if not tasks_by_status['queued_for_claude']:
        cols = [c for c in cols if c != 'queued_for_claude']

    tasks_needing_approval = [t for t in tasks if int(t["requires_approval"] or 0) == 1 and t["status"] == "pending"]

    health_state = claude_health.get_state() if CLAUDE_CLI_ENABLED else "HEALTHY"

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "cols": cols,
            "tasks_by_status": tasks_by_status,
            "tasks_needing_approval": tasks_needing_approval,
            "agents": agents,
            "now": utcnow_iso(),
            "claude_health_state": health_state,
            "paused_count": paused_count,
            "queued_count": queued_count,
        },
    )


@app.post("/tasks")
def create_task(
    title: str = Form(...),
    description: str = Form(""),
    due_date: str = Form(""),
    assigned_agent_id: str = Form(""),
    is_critical: Optional[str] = Form(None),
    schedule_type: Optional[str] = Form(None),
    interval_minutes: Optional[str] = Form(None),
    cron_expr: Optional[str] = Form(None),
    is_recurring: Optional[str] = Form(None),
) -> RedirectResponse:
    dd = due_date.strip() or None
    aid = int(assigned_agent_id) if assigned_agent_id else None
    critical = 1 if is_critical else 0
    schedule_type = (schedule_type or "none").strip()
    cron_expr = (cron_expr or "").strip() or None
    interval_m = int(interval_minutes) if (interval_minutes or "").strip().isdigit() else None
    recurring = 1 if is_recurring else 0
    requires_approval = 1
    next_run = None
    if schedule_type == "interval" and interval_m and interval_m > 0:
        next_run = (datetime.now(timezone.utc) + timedelta(minutes=interval_m)).replace(microsecond=0).isoformat()
    elif schedule_type == "cron" and cron_expr:
        try:
            it = croniter(cron_expr, datetime.now(timezone.utc))
            next_run = it.get_next(datetime).replace(microsecond=0).isoformat()
        except Exception:
            next_run = None

    con = connect()
    con.execute(
        """
        INSERT INTO tasks(title, description, status, assigned_agent_id, due_date, is_critical, requires_approval, created_at, updated_at, schedule_type, cron_expr, interval_minutes, is_recurring, next_run_at)
        VALUES(?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (title, description, aid, dd, critical, requires_approval, utcnow_iso(), utcnow_iso(), schedule_type, cron_expr, interval_m, recurring, next_run),
    )
    con.commit()
    con.close()

    _log("task_created", "task", None, title)

    try:
        if critical == 1 and _env_bool('AUTO_EMAIL_APPROVAL_ON_CREATE', True):
            con = connect()
            row = con.execute("SELECT id FROM tasks ORDER BY id DESC LIMIT 1").fetchone()
            con.close()
            if row:
                _request_approval_for_task(int(row['id']))
    except Exception as e:
        _log('approval_email_error', 'task', None, str(e))

    return RedirectResponse(url="/dashboard", status_code=303)


@app.get("/tasks/new", response_class=HTMLResponse)
def new_task_page(request: Request) -> HTMLResponse:
    con = connect()
    agents = con.execute("SELECT * FROM agents ORDER BY name").fetchall()
    con.close()
    return templates.TemplateResponse("tasks_new.html", {"request": request, "agents": agents})

@app.get("/task/new")
def new_task_alias() -> RedirectResponse:
    return RedirectResponse(url="/tasks/new", status_code=302)



@app.get("/tasks/{task_id}", response_class=HTMLResponse)
def task_detail(request: Request, task_id: int) -> HTMLResponse:
    con = connect()
    task = con.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    agents = con.execute("SELECT * FROM agents WHERE is_active=1 ORDER BY name").fetchall()
    executor_logs = con.execute(
        "SELECT * FROM executor_log WHERE task_id=? ORDER BY id ASC", (task_id,)
    ).fetchall()
    con.close()

    if not task:
        return HTMLResponse("Task not found", status_code=404)

    statuses = ["pending", "approved", "rejected", "active", "running", "completed", "blocked", "paused_limit", "queued_for_claude", "dev_done", "review", "done"]
    return templates.TemplateResponse(
        "task_detail.html",
        {"request": request, "task": task, "agents": agents, "statuses": statuses, "now": utcnow_iso(), "executor_logs": executor_logs},
    )


@app.post("/tasks/{task_id}/update")
def task_update(
    task_id: int,
    status: str = Form(...),
    assigned_agent_id: str = Form(""),
    due_date: str = Form(""),
    is_critical: Optional[str] = Form(None),
    description: str = Form(""),
) -> RedirectResponse:
    con = connect()

    agent_raw = (assigned_agent_id or "").strip()
    aid = None
    if agent_raw:
        if agent_raw.isdigit():
            aid = int(agent_raw)
        else:
            row = con.execute("SELECT id FROM agents WHERE name=?", (agent_raw,)).fetchone()
            if row:
                aid = int(row["id"])
            else:
                _log("task_update_bad_agent", "task", str(task_id), f"agent_raw={agent_raw}")

    dd = due_date.strip() or None
    critical = 1 if is_critical else 0

    cur_task = con.execute("SELECT id, requires_approval, status FROM tasks WHERE id=?", (task_id,)).fetchone()
    if not cur_task:
        con.close()
        return RedirectResponse(url="/dashboard", status_code=303)

    if int(cur_task["requires_approval"] or 0) == 1 and status in ("approved", "rejected") and str(cur_task["status"]) not in ("approved", "rejected"):
        con.close()
        return HTMLResponse("This task requires approval; use the approval link (email) or the board approval flow.", status_code=403)

    con.execute(
        """
        UPDATE tasks
        SET status=?, assigned_agent_id=?, due_date=?, is_critical=?, description=?, updated_at=?
        WHERE id=?
        """,
        (status, aid, dd, critical, description, utcnow_iso(), task_id),
    )
    con.commit()
    con.close()

    _log("task_updated", "task", str(task_id), f"status={status} assigned_agent_id={aid}")
    return RedirectResponse(url=f"/tasks/{task_id}", status_code=303)


@app.post("/tasks/{task_id}/delete")
def task_delete(task_id: int) -> RedirectResponse:
    con = connect()
    t = con.execute("SELECT title FROM tasks WHERE id=?", (task_id,)).fetchone()
    title = t["title"] if t else ""

    try:
        con.execute("DELETE FROM decisions WHERE entity_type='task' AND entity_id=?", (task_id,))
    except Exception:
        pass
    try:
        con.execute("DELETE FROM critiques WHERE task_id=?", (str(task_id),))
    except Exception:
        pass
    try:
        con.execute("DELETE FROM executor_log WHERE task_id=?", (task_id,))
    except Exception:
        pass

    con.execute("DELETE FROM tasks WHERE id=?", (task_id,))
    con.commit()
    con.close()

    _log("task_deleted", "task", str(task_id), title)
    return RedirectResponse(url="/dashboard", status_code=303)


@app.get("/decision/{decision_id}", response_class=HTMLResponse)
def decision_view(request: Request, decision_id: str) -> HTMLResponse:
    con = connect()
    d = con.execute("SELECT * FROM decisions WHERE decision_id=?", (decision_id,)).fetchone()
    con.close()
    if not d:
        return HTMLResponse("Decision not found", status_code=404)
    return _decision_result_page(request, dict(d), title=f"Decision: {dict(d).get('status','')}")


@app.post("/api/tasks/move")
async def api_tasks_move(request: Request, payload: dict) -> JSONResponse:
    try:
        task_id = int(payload.get('task_id'))
    except Exception:
        return JSONResponse({'ok': False, 'error': 'bad_task_id'}, status_code=400)
    new_status = str(payload.get('to_status') or payload.get('status') or '').strip()
    allowed = {'pending','approved','rejected','active','running','completed','blocked','dev_done','review','done','paused_limit','queued_for_claude'}
    if new_status not in allowed:
        return JSONResponse({'ok': False, 'error': 'bad_status'}, status_code=400)

    con = connect()
    task = con.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    con.close()
    if not task:
        return JSONResponse({'ok': False, 'error': 'not_found'}, status_code=404)

    req_appr = int(task['requires_approval'] or 0)
    old_status = task['status']

    if req_appr == 1 and old_status == 'pending' and new_status == 'active':
        return JSONResponse({'ok': False, 'error': 'must_be_approved_first'}, status_code=403)

    if new_status in ('approved','rejected') and req_appr == 1:
        if not _env_bool('DASHBOARD_APPROVALS_ENABLED', False):
            return JSONResponse({'ok': False, 'error': 'dashboard_approvals_disabled'}, status_code=403)

        approve = (new_status == 'approved')
        md = (
            f"# Board Approval\n\n"
            f"**Task:** {task['title']} (ID {task['id']})\n\n"
            f"**From:** {old_status}\n"
            f"**To:** {new_status}\n\n"
            f"**Actor:** dashboard user\n"
            f"**IP:** {(request.client.host if request.client else '')}\n"
        )
        decision_id, token = create_decision(
            entity_type='task',
            entity_id=task_id,
            action='board_approve' if approve else 'board_reject',
            requester='dashboard',
            ttl_hours=int(os.getenv('APPROVAL_TTL_HOURS', '72')),
            result_markdown=md,
        )
        try:
            await _upstream_call('/approve' if approve else '/reject', decision_id, token)
        except Exception as e:
            _log('upstream_call_error', 'decision', decision_id, str(e))

        apply_decision(
            decision_id=decision_id,
            approve=approve,
            decider_ip=(request.client.host if request.client else None),
            decider_ua=request.headers.get('user-agent'),
        )
        return JSONResponse({'ok': True, 'decision_id': decision_id, 'decision_view_url': f"/decision/{decision_id}"})

    con = connect()
    con.execute("UPDATE tasks SET status=?, updated_at=? WHERE id=?", (new_status, utcnow_iso(), task_id))
    con.commit()
    con.close()
    _log('task_moved_board', 'task', str(task_id), f"{old_status}->{new_status}")
    return JSONResponse({'ok': True})


@app.post("/tasks/{task_id}/complete")
def task_complete(task_id: int) -> RedirectResponse:
    con = connect()
    task = con.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    con.close()
    if not task:
        return RedirectResponse(url="/dashboard", status_code=303)

    if int(task["requires_approval"] or 0) != 1 or task["status"] != 'pending':
        return RedirectResponse(url=f"/tasks/{task_id}", status_code=303)

    _request_approval_for_task(task_id)
    _log("approval_email_resent", "task", str(task_id), task["title"])
    return RedirectResponse(url=f"/tasks/{task_id}", status_code=303)


def _request_approval_for_task(task_id: int) -> None:
    con = connect()
    task = con.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    con.close()
    if not task:
        return

    if task['status'] != 'pending':
        return

    con2 = connect()
    con2.execute("UPDATE decisions SET status='superseded', updated_at=? WHERE entity_type='task' AND entity_id=? AND action='start_task' AND status='pending'", (utcnow_iso(), task_id))
    con2.commit()
    con2.close()

    if not APPROVER_EMAIL:
        _log("approval_skipped", "task", str(task_id), "Approver email not configured")
        return

    md = f"""# Approval Request\n\n**Task:** {task['title']} (ID {task['id']})\n\n## Description\n\n{task['description']}\n\n## Requested action\n\nApprove this task to move it into the Approved column (pre-work approval).\n"""

    decision_id, token = create_decision(
        entity_type="task",
        entity_id=task_id,
        action="start_task",
        requester="zeroclaw",
        ttl_hours=int(os.getenv("APPROVAL_TTL_HOURS", "72")),
        result_markdown=md,
    )

    approve_url = f"{PUBLIC_BASE_URL}/approve?decision_id={decision_id}&token={token}"
    reject_url = f"{PUBLIC_BASE_URL}/reject?decision_id={decision_id}&token={token}"

    html = f"""
    <h2>Approval needed</h2>
    <p><b>Task:</b> {task['title']} (ID {task['id']})</p>
    <p>{task['description']}</p>
    <p>
      <a href="{approve_url}" style="display:inline-block;padding:10px 14px;background:#16a34a;color:white;text-decoration:none;border-radius:6px;">Approve</a>
      &nbsp;
      <a href="{reject_url}" style="display:inline-block;padding:10px 14px;background:#dc2626;color:white;text-decoration:none;border-radius:6px;">Reject</a>
    </p>
    <p>Decision ID: <code>{decision_id}</code></p>
    <p>Task page: <a href="{PUBLIC_BASE_URL}/tasks/{task['id']}">{PUBLIC_BASE_URL}/tasks/{task['id']}</a></p>
    """

    send_email(to_addr=APPROVER_EMAIL, subject=f"[ZeroClaw] Approve task to start: {task['title']}", html_body=html)
    _log("approval_email_sent", "decision", decision_id, f"task_id={task_id} to={APPROVER_EMAIL}")


# ── Agent endpoints ──

@app.get("/agents", response_class=HTMLResponse)
def agents(request: Request) -> HTMLResponse:
    con = connect()
    agents_ = con.execute("SELECT * FROM agents ORDER BY updated_at DESC").fetchall()
    running_rows = con.execute("""
        SELECT assigned_agent_id AS agent_id, COUNT(*) AS c
        FROM tasks
        WHERE assigned_agent_id IS NOT NULL
          AND (openclaw_job_status IN ('queued','running') OR status IN ('active','running'))
        GROUP BY assigned_agent_id
    """,).fetchall()
    running_by_agent = {str(r['agent_id']): int(r['c']) for r in running_rows}
    con.close()
    roles = ["programming", "reporting", "reviewing", "architecture", "general"]

    # Add agent file info
    agents_with_files = []
    for a in agents_:
        a_dict = dict(a)
        a_dict['files'] = list_agent_files(a['name'])
        agents_with_files.append(a_dict)

    return templates.TemplateResponse("agents.html", {"request": request, "agents": agents_with_files,
            "running_by_agent": running_by_agent, "roles": roles, "now": utcnow_iso()})


@app.post("/agents")
@app.post("/agents/create")
def create_agent(name: str = Form(...), role: str = Form("general"), model: str = Form("openai/gpt-5.2")) -> RedirectResponse:
    con = connect()
    con.execute(
        "INSERT INTO agents(name, role, model, is_active, created_at, updated_at) VALUES(?,?,?,?,?,?)",
        (name, role, model, 1, utcnow_iso(), utcnow_iso()),
    )
    con.commit()
    con.close()
    # Create agent internal files
    ensure_agent_dir(name, role)
    _log("agent_created", "agent", None, name)
    return RedirectResponse(url="/agents", status_code=303)


@app.get("/agents/{agent_id}", response_class=HTMLResponse)
def agent_detail(request: Request, agent_id: int) -> HTMLResponse:
    con = connect()
    a = con.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone()
    pipelines = con.execute("SELECT id, name, task_type FROM pipelines WHERE is_active=1 ORDER BY name").fetchall()
    con.close()
    if not a:
        return HTMLResponse("Agent not found", status_code=404)
    roles = ["programming", "reporting", "reviewing", "architecture", "general"]

    # Ensure agent files exist
    ensure_agent_dir(a['name'], a['role'])
    agent_files = list_agent_files(a['name'])
    file_contents = {f: read_agent_file(a['name'], f) for f in agent_files}

    return templates.TemplateResponse("agent_detail.html", {
        "request": request, "agent": a, "roles": roles, "now": utcnow_iso(),
        "pipelines": pipelines, "agent_files": agent_files, "file_contents": file_contents,
    })


@app.post("/agents/{agent_id}/update")
def agent_update(agent_id: int, name: str = Form(...), role: str = Form("general"), pipeline_id: str = Form(""), is_active: Optional[str] = Form(None)) -> RedirectResponse:
    active = 1 if is_active else 0
    pid = int(pipeline_id) if pipeline_id.strip() else None
    con = connect()
    # Check if name changed for file rename
    old = con.execute("SELECT name FROM agents WHERE id=?", (agent_id,)).fetchone()
    old_name = old['name'] if old else name
    con.execute(
        "UPDATE agents SET name=?, role=?, pipeline_id=?, is_active=?, updated_at=? WHERE id=?",
        (name, role, pid, active, utcnow_iso(), agent_id),
    )
    con.commit()
    con.close()
    if old_name != name:
        rename_agent_dir(old_name, name)
    ensure_agent_dir(name, role)
    _log("agent_updated", "agent", str(agent_id), f"role={role} pipeline_id={pid}")
    return RedirectResponse(url=f"/agents/{agent_id}", status_code=303)


@app.post("/agents/{agent_id}/files/{filename}")
def agent_file_save(agent_id: int, filename: str, content: str = Form("")) -> RedirectResponse:
    con = connect()
    a = con.execute("SELECT name FROM agents WHERE id=?", (agent_id,)).fetchone()
    con.close()
    if not a:
        return RedirectResponse(url="/agents", status_code=303)
    write_agent_file(a['name'], filename, content)
    _log("agent_file_saved", "agent", str(agent_id), filename)
    return RedirectResponse(url=f"/agents/{agent_id}", status_code=303)


@app.post("/agents/{agent_id}/files/new")
def agent_file_new(agent_id: int, filename: str = Form(...)) -> RedirectResponse:
    con = connect()
    a = con.execute("SELECT name, role FROM agents WHERE id=?", (agent_id,)).fetchone()
    con.close()
    if not a:
        return RedirectResponse(url="/agents", status_code=303)
    if not filename.endswith('.md'):
        filename = filename + '.md'
    write_agent_file(a['name'], filename, f"# {a['name']} — {filename}\n\n")
    return RedirectResponse(url=f"/agents/{agent_id}", status_code=303)


@app.get("/agents/{agent_id}/files/{filename}/delete")
def agent_file_delete_route(agent_id: int, filename: str) -> RedirectResponse:
    con = connect()
    a = con.execute("SELECT name FROM agents WHERE id=?", (agent_id,)).fetchone()
    con.close()
    if a:
        delete_agent_file(a['name'], filename)
    return RedirectResponse(url=f"/agents/{agent_id}", status_code=303)


@app.post("/api/agent_report")
async def api_agent_report(payload: dict) -> JSONResponse:
    agent_id = payload.get("agent_id")
    msg = payload.get("message", "")
    task_id = payload.get("task_id")
    _log("agent_report", "agent", str(agent_id), f"task={task_id} msg={msg}")
    return JSONResponse({"ok": True})


# ── Pipeline endpoints ──

@app.get("/pipelines", response_class=HTMLResponse)
def pipelines_page(request: Request, selected: int = 0) -> HTMLResponse:
    con = connect()
    pipelines = con.execute("SELECT * FROM pipelines ORDER BY updated_at DESC").fetchall()
    selected_pipeline = None
    if selected:
        selected_pipeline = con.execute("SELECT * FROM pipelines WHERE id=?", (selected,)).fetchone()
    elif pipelines:
        selected_pipeline = pipelines[0]
    con.close()

    return templates.TemplateResponse("pipelines.html", {
        "request": request,
        "pipelines": pipelines,
        "selected_pipeline": dict(selected_pipeline) if selected_pipeline else None,
    })


@app.post("/pipelines/create")
def pipeline_create() -> RedirectResponse:
    con = connect()
    con.execute(
        "INSERT INTO pipelines(name, description, task_type, blocks_json, is_active, created_at, updated_at) VALUES(?,?,?,?,?,?,?)",
        ("New Pipeline", "", "default", "[]", 0, utcnow_iso(), utcnow_iso()),
    )
    new_id = con.execute("SELECT last_insert_rowid() AS x").fetchone()["x"]
    con.commit()
    con.close()
    _log("pipeline_created", "pipeline", str(new_id), "new pipeline")
    return RedirectResponse(url=f"/pipelines?selected={new_id}", status_code=303)


@app.post("/pipelines/{pipeline_id}/update")
def pipeline_update(
    pipeline_id: int,
    name: str = Form(...),
    task_type: str = Form(""),
    description: str = Form(""),
    blocks_json: str = Form(""),
    blocks_json_raw: str = Form(""),
    is_active: Optional[str] = Form(None),
) -> RedirectResponse:
    active = 1 if is_active else 0
    # Prefer blocks_json (from visual editor hidden field), fall back to blocks_json_raw (JSON textarea)
    bj = blocks_json.strip() or blocks_json_raw.strip() or "[]"
    # Validate JSON
    try:
        json.loads(bj)
    except Exception:
        bj = "[]"

    con = connect()
    con.execute(
        "UPDATE pipelines SET name=?, description=?, task_type=?, blocks_json=?, is_active=?, updated_at=? WHERE id=?",
        (name, description, task_type.strip() or "default", bj, active, utcnow_iso(), pipeline_id),
    )
    con.commit()
    con.close()
    _log("pipeline_updated", "pipeline", str(pipeline_id), name)
    return RedirectResponse(url=f"/pipelines?selected={pipeline_id}", status_code=303)


@app.get("/pipelines/{pipeline_id}/delete")
def pipeline_delete(pipeline_id: int) -> RedirectResponse:
    con = connect()
    con.execute("DELETE FROM pipelines WHERE id=?", (pipeline_id,))
    con.commit()
    con.close()
    _log("pipeline_deleted", "pipeline", str(pipeline_id), "")
    return RedirectResponse(url="/pipelines", status_code=303)


# ── Claude Health API ──

@app.post("/api/claude-health/reset")
def claude_health_reset() -> RedirectResponse:
    claude_health.manual_reset()
    _log("claude_health_reset", "system", None, "manual reset from dashboard")
    return RedirectResponse(url="/dashboard", status_code=303)


@app.get("/api/claude-health")
def claude_health_status() -> JSONResponse:
    return JSONResponse(claude_health.get_full_status())


# ── Critiques ──

@app.get("/critiques", response_class=HTMLResponse)
def critiques(request: Request) -> HTMLResponse:
    con = connect()
    crits = con.execute("SELECT * FROM critiques ORDER BY created_at DESC LIMIT 200").fetchall()
    tasks = con.execute("SELECT id,title FROM tasks").fetchall()
    con.close()
    task_map = {t["id"]: t["title"] for t in tasks}
    return templates.TemplateResponse("critiques.html", {"request": request, "critiques": crits, "task_map": task_map, "now": utcnow_iso()})


@app.post("/critiques")
def create_critique(title: str = Form(...), body: str = Form(""), severity: str = Form("medium"), task_id: str = Form("")) -> RedirectResponse:
    tid = int(task_id) if task_id else None
    con = connect()
    con.execute(
        "INSERT INTO critiques(task_id, title, body, severity, created_at) VALUES(?,?,?,?,?)",
        (tid, title, body, severity, utcnow_iso()),
    )
    con.commit()
    con.close()
    _log("critique_created", "critique", None, f"severity={severity} task_id={tid} title={title}")
    return RedirectResponse(url="/critiques", status_code=303)


# ── Logs ──

@app.get("/logs", response_class=HTMLResponse)
def logs(request: Request) -> HTMLResponse:
    con = connect()
    logs_ = con.execute("SELECT * FROM action_logs ORDER BY ts DESC LIMIT 300").fetchall()
    agents_raw = con.execute("SELECT id, name, role FROM agents").fetchall()
    agents_by_id = {str(dict(a)["id"]): dict(a) for a in agents_raw}
    task_agents = {}
    for row in con.execute("SELECT id, assigned_agent_id FROM tasks WHERE assigned_agent_id IS NOT NULL").fetchall():
        r = dict(row)
        aid = str(r["assigned_agent_id"])
        if aid in agents_by_id:
            task_agents[str(r["id"])] = agents_by_id[aid]
    con.close()
    _role_map = {"programmer": "Ada", "programming": "Ada", "architect": "Jorven", "architecture": "Jorven", "reviewer": "Iris", "reviewing": "Iris", "reporter": "Quimby", "reporting": "Quimby"}
    return templates.TemplateResponse("logs.html", {"request": request, "logs": logs_, "now": utcnow_iso(), "task_agents": task_agents, "agents_by_id": agents_by_id, "role_map": _role_map})


# ── Decision pages ──

def _decision_result_page(request: Request, decision: dict, *, title: str, note: str = "") -> HTMLResponse:
    rendered = markdown2.markdown(decision.get("result_markdown", ""))
    return templates.TemplateResponse(
        "decision_result.html",
        {
            "request": request,
            "title": title,
            "note": note,
            "decision": decision,
            "rendered_html": rendered,
            "raw_md": decision.get("result_markdown", ""),
            "now": utcnow_iso(),
        },
    )


async def _upstream_call(path: str, decision_id: str, token: str) -> Optional[dict]:
    if not APPROVAL_UPSTREAM_BASE_URL:
        return None
    url = f"{APPROVAL_UPSTREAM_BASE_URL}{path}"
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(url, params={"decision_id": decision_id, "token": token})
        return {"status_code": r.status_code, "text": r.text}


@app.get("/approve", response_class=HTMLResponse)
async def approve(request: Request, decision_id: str, token: str) -> HTMLResponse:
    d = verify_decision_token(decision_id=decision_id, token=token)
    if not d:
        return HTMLResponse("Invalid or expired token", status_code=403)

    d2 = apply_decision(
        decision_id=decision_id,
        approve=True,
        decider_ip=request.client.host if request.client else None,
        decider_ua=request.headers.get("user-agent"),
    )

    upstream = await _upstream_call("/approve", decision_id, token)
    note = f"Upstream: {upstream}" if upstream else ""
    return _decision_result_page(request, d2 or d, title="Approved", note=note)


@app.get("/reject", response_class=HTMLResponse)
async def reject(request: Request, decision_id: str, token: str) -> HTMLResponse:
    d = verify_decision_token(decision_id=decision_id, token=token)
    if not d:
        return HTMLResponse("Invalid or expired token", status_code=403)

    d2 = apply_decision(
        decision_id=decision_id,
        approve=False,
        decider_ip=request.client.host if request.client else None,
        decider_ua=request.headers.get("user-agent"),
    )

    upstream = await _upstream_call("/reject", decision_id, token)
    note = f"Upstream: {upstream}" if upstream else ""
    return _decision_result_page(request, d2 or d, title="Rejected", note=note)


@app.get("/status")
async def status(decision_id: str, token: str) -> JSONResponse:
    d = verify_decision_token(decision_id=decision_id, token=token)
    if not d:
        return JSONResponse({"ok": False, "error": "invalid_or_expired"}, status_code=403)

    upstream = None
    if APPROVAL_UPSTREAM_BASE_URL:
        upstream = await _upstream_call("/status", decision_id, token)

    return JSONResponse(
        {
            "ok": True,
            "decision_id": d["decision_id"],
            "status": d["status"],
            "entity_type": d["entity_type"],
            "entity_id": d["entity_id"],
            "action": d["action"],
            "expires_at": d["expires_at"],
            "upstream": upstream,
        }
    )


# ── Scheduler logic ──

def _compute_next_run(schedule_type: str, cron_expr: str | None, interval_minutes: int | None, base_dt: datetime) -> str | None:
    schedule_type = (schedule_type or 'none').strip()
    if schedule_type == 'interval' and interval_minutes and int(interval_minutes) > 0:
        return (base_dt + timedelta(minutes=int(interval_minutes))).replace(microsecond=0).isoformat()
    if schedule_type == 'cron' and cron_expr:
        try:
            it = croniter(cron_expr, base_dt)
            return it.get_next(datetime).replace(microsecond=0).isoformat()
        except Exception:
            return None
    return None


def _tick_scheduled_tasks() -> None:
    now = datetime.now(timezone.utc).replace(microsecond=0)

    con = connect()
    # 1) request approvals for scheduled tasks within lead time
    rows = con.execute(
        """
        SELECT id,next_run_at FROM tasks
        WHERE schedule_type!='none' AND status='pending' AND requires_approval=1 AND next_run_at IS NOT NULL
        """
    ).fetchall()
    con.close()

    for r in rows:
        try:
            nxt = datetime.fromisoformat(r['next_run_at'])
        except Exception:
            continue
        if (nxt - now).total_seconds() > SCHEDULE_APPROVAL_LEAD_SECONDS:
            continue
        con2 = connect()
        d = con2.execute(
            "SELECT decision_id FROM decisions WHERE entity_type='task' AND entity_id=? AND action='start_task' AND status='pending'",
            (r['id'],),
        ).fetchone()
        con2.close()
        if not d:
            _request_approval_for_task(int(r['id']))

    # 2) dispatch approved tasks via pipeline executor
    con = connect()
    due = con.execute(
        """
        SELECT * FROM tasks
        WHERE status='approved'
          AND (openclaw_job_id IS NULL OR openclaw_job_id='')
        """
    ).fetchall()

    for t in due:
        t = dict(t)
        # non-scheduled tasks dispatch immediately; scheduled dispatch when due
        if (t.get('schedule_type') or 'none') != 'none':
            if not t.get('next_run_at'):
                continue
            try:
                nxt = datetime.fromisoformat(t['next_run_at'])
            except Exception:
                continue
            if nxt > now:
                continue

        if not t.get('assigned_agent_id'):
            con.execute("UPDATE tasks SET last_error=?, updated_at=? WHERE id=?", ('no_assigned_agent', utcnow_iso(), t['id']))
            con.commit()
            _log('dispatch_skipped', 'task', str(t['id']), 'no assigned_agent_id')
            continue

        # Use pipeline executor (runs in background thread)
        task_id = t['id']
        _log("pipeline_dispatch", "task", str(task_id), f"dispatching via pipeline executor")

        def _run_pipeline_thread(tid):
            try:
                run_pipeline(tid)
            except Exception as e:
                _log("pipeline_error", "task", str(tid), str(e))

        thread = threading.Thread(target=_run_pipeline_thread, args=(task_id,), daemon=True)
        thread.start()

        # Mark task as active so it's not re-dispatched
        con.execute("UPDATE tasks SET status='active', updated_at=? WHERE id=?", (utcnow_iso(), task_id))
        con.commit()

    con.close()


def _poll_openclaw_jobs() -> None:
    """Poll OpenClaw jobs that were dispatched via the legacy path."""
    now = datetime.now(timezone.utc).replace(microsecond=0)
    con = connect()
    rows = con.execute(
        """
        SELECT * FROM tasks
        WHERE openclaw_job_id IS NOT NULL AND openclaw_job_id!=''
          AND status IN ('active','running')
        """
    ).fetchall()

    for t in rows:
        t = dict(t)
        is_review_task = str(t.get('title') or '').strip().lower().startswith('review:') or ('[review_of_task_id:' in str(t.get('description') or '').lower())
        is_resolve_task = str(t.get('title') or '').strip().lower().startswith('resolve:')
        job_id = str(t['openclaw_job_id'])
        res = get_status(job_id=job_id)
        if not res.get('ok'):
            con.execute('UPDATE tasks SET last_error=?, updated_at=? WHERE id=?', (res.get('error'), utcnow_iso(), t['id']))
            con.execute('INSERT INTO action_logs(ts, action, entity_type, entity_id, detail, layer, model) VALUES(?,?,?,?,?,?,?)', (utcnow_iso(), 'openclaw_status_error', 'task', str(t['id']), res.get('error') or '', 'openclaw', None))
            con.commit()
            continue

        payload = res.get('raw') or {}
        state = normalize_state(payload)
        if state == 'queued':
            new_status = 'active'
        elif state == 'running':
            new_status = 'running'
        elif state == 'completed':
            new_status = 'completed'
        elif state == 'failed':
            new_status = 'blocked'
        else:
            new_status = t['status']

        last_result = ''
        if isinstance(payload, dict):
            last_result = str(payload.get('result') or payload.get('output') or payload.get('message') or '')

        con.execute(
            'UPDATE tasks SET status=?, openclaw_job_status=?, openclaw_last_status_payload=?, last_result=?, updated_at=? WHERE id=?',
            (new_status, state, json.dumps(payload), (last_result or (t.get('last_result') or '')), utcnow_iso(), t['id'])
        )

        if new_status in ('completed','blocked'):
            con.execute('UPDATE tasks SET openclaw_job_id=NULL WHERE id=?', (t['id'],))

            if (t.get('schedule_type') or 'none') != 'none' and int(t.get('is_recurring') or 0) == 1:
                nxt = _compute_next_run(t.get('schedule_type') or 'none', t.get('cron_expr'), t.get('interval_minutes'), now)
                con.execute("UPDATE tasks SET status='pending', next_run_at=?, openclaw_job_status=NULL, updated_at=? WHERE id=?", (nxt, utcnow_iso(), t['id']))
                con.execute('INSERT INTO action_logs(ts, action, entity_type, entity_id, detail) VALUES(?,?,?,?,?)', (utcnow_iso(), 'task_rescheduled', 'task', str(t['id']), f'next_run_at={nxt}'))
            else:
                if is_review_task:
                    if state == 'failed':
                        review_output = last_result or (t.get('last_result') or '')
                        con.execute("UPDATE tasks SET status='approved', last_error='review_job_failed', updated_at=? WHERE id=?", (utcnow_iso(), t['id']))
                        if review_output:
                            desc = str(t.get('description') or '')
                            m2 = re.search(r'\[review_of_task_id:(\d+)\]', desc)
                            if m2:
                                src_id2 = int(m2.group(1))
                                con.execute("UPDATE tasks SET review_summary=?, updated_at=? WHERE id=?", (review_output, utcnow_iso(), src_id2))
                    else:
                        review_output = last_result or (t.get('last_result') or '')
                        desc = str(t.get('description') or '')
                        m = re.search(r'\[review_of_task_id:(\d+)\]', desc)
                        src_attached = False
                        review_pass = True
                        if review_output:
                            low = review_output.lower()
                            if '"verdict"' in low and '"fail"' in low:
                                review_pass = False
                            elif low.strip().startswith('fail') or '\nfail' in low:
                                review_pass = False
                        if m:
                            src_id = int(m.group(1))
                            src = con.execute('SELECT id, status FROM tasks WHERE id=?', (src_id,)).fetchone()
                            if src:
                                src_dict = dict(src)
                                con.execute("UPDATE tasks SET review_summary=?, updated_at=? WHERE id=?", (review_output, utcnow_iso(), src_id))
                                if review_pass and src_dict['status'] in ('dev_done', 'review'):
                                    con.execute("UPDATE tasks SET status='done', updated_at=? WHERE id=?", (utcnow_iso(), src_id))
                                elif not review_pass and src_dict['status'] in ('dev_done', 'review'):
                                    con.execute("UPDATE tasks SET status='approved', retry_count=0, openclaw_job_id=NULL, openclaw_job_status=NULL, updated_at=? WHERE id=?", (utcnow_iso(), src_id))
                                src_attached = True
                        con.execute("UPDATE tasks SET status='done', updated_at=? WHERE id=?", (utcnow_iso(), t['id']))
                        if review_pass and src_attached:
                            con.execute("DELETE FROM decisions WHERE entity_type='task' AND entity_id=?", (t['id'],))
                            con.execute('DELETE FROM critiques WHERE task_id=?', (t['id'],))
                            con.execute('DELETE FROM tasks WHERE id=?', (t['id'],))
                elif is_resolve_task:
                    resolve_output = last_result or (t.get('last_result') or '')
                    desc = str(t.get('description') or '')
                    m3 = re.search(r'\[resolve_blocked_task_id:(\d+)\]', desc)
                    if state == 'failed':
                        con.execute("DELETE FROM decisions WHERE entity_type='task' AND entity_id=?", (t['id'],))
                        con.execute('DELETE FROM critiques WHERE task_id=?', (t['id'],))
                        con.execute('DELETE FROM tasks WHERE id=?', (t['id'],))
                    else:
                        if m3:
                            src_id3 = int(m3.group(1))
                            src3 = con.execute('SELECT id, status FROM tasks WHERE id=?', (src_id3,)).fetchone()
                            if src3 and dict(src3)['status'] == 'blocked':
                                con.execute("UPDATE tasks SET status='approved', retry_count=0, last_error=NULL, last_result=?, openclaw_job_id=NULL, openclaw_job_status=NULL, updated_at=? WHERE id=?", (resolve_output, utcnow_iso(), src_id3))
                        con.execute("DELETE FROM decisions WHERE entity_type='task' AND entity_id=?", (t['id'],))
                        con.execute('DELETE FROM critiques WHERE task_id=?', (t['id'],))
                        con.execute('DELETE FROM tasks WHERE id=?', (t['id'],))
                else:
                    if state == 'failed':
                        con.execute("UPDATE tasks SET status='blocked', last_error='openclaw_job_failed', updated_at=? WHERE id=?", (utcnow_iso(), t['id']))
                    else:
                        con.execute("UPDATE tasks SET status='dev_done', updated_at=? WHERE id=?", (utcnow_iso(), t['id']))

        con.execute('INSERT INTO action_logs(ts, action, entity_type, entity_id, detail, layer, model) VALUES(?,?,?,?,?,?,?)', (utcnow_iso(), 'openclaw_polled', 'task', str(t['id']), f'state={state}', 'openclaw', str((payload.get('used_model') or payload.get('agent_model') or payload.get('model') or '')) or None))
        con.commit()

    con.close()
