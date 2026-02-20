from __future__ import annotations

import os
import secrets
from typing import Optional

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from .db import connect
from .openclaw import dispatch_job

BASE_DIR = os.path.dirname(__file__)
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

router = APIRouter()


def _env_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None or v == "":
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _utcnow_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _log(action: str, entity_type: str, entity_id: str | None, detail: str) -> None:
    # Best-effort logging.
    try:
        con = connect()
        con.execute(
            "INSERT INTO action_logs(ts, action, entity_type, entity_id, detail) VALUES(?,?,?,?,?)",
            (_utcnow_iso(), action, entity_type, entity_id, detail),
        )
        con.commit()
        con.close()
    except Exception:
        pass


def ensure_default_routine() -> None:
    con = connect()
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS routines(
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            kind TEXT NOT NULL,
            is_enabled INTEGER NOT NULL DEFAULT 0,
            agent_id INTEGER NULL,
            claim_unassigned INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )

    # Seed idle_autostart
    idle = con.execute("SELECT id FROM routines WHERE kind='idle_autostart' LIMIT 1").fetchone()
    if not idle:
        rid = secrets.token_hex(8)
        con.execute(
            "INSERT INTO routines(id,name,kind,is_enabled,agent_id,claim_unassigned,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                rid,
                "Auto-start next approved task when idle",
                "idle_autostart",
                0,
                None,
                0,
                _utcnow_iso(),
                _utcnow_iso(),
            ),
        )
        con.commit()
        _log("routine_seeded", "routine", rid, "idle_autostart_disabled_by_default")

    # Seed review_autocreate
    rev = con.execute("SELECT id FROM routines WHERE kind='review_autocreate' LIMIT 1").fetchone()
    if not rev:
        rid2 = secrets.token_hex(8)
        con.execute(
            "INSERT INTO routines(id,name,kind,is_enabled,agent_id,claim_unassigned,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                rid2,
                "Auto-create approved review tasks for items in Dev Done",
                "review_autocreate",
                0,
                None,
                0,
                _utcnow_iso(),
                _utcnow_iso(),
            ),
        )
        con.commit()
        _log("routine_seeded", "routine", rid2, "review_autocreate_disabled_by_default")

    # Seed status_report_email
    rep = con.execute("SELECT id FROM routines WHERE kind='status_report_email' LIMIT 1").fetchone()
    if not rep:
        rid3 = secrets.token_hex(8)
        con.execute(
            "INSERT INTO routines(id,name,kind,is_enabled,agent_id,claim_unassigned,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                rid3,
                "Status report email (10 completions, 30min min)",
                "status_report_email",
                0,
                None,
                0,
                _utcnow_iso(),
                _utcnow_iso(),
            ),
        )
        con.commit()
        _log("routine_seeded", "routine", rid3, "status_report_email_disabled_by_default")

    con.close()


def agent_is_running(con, agent_id: int) -> bool:
    row = con.execute(
        """
        SELECT COUNT(*) AS c
        FROM tasks
        WHERE assigned_agent_id=?
          AND (
            openclaw_job_status IN ('queued','running')
            OR status IN ('active','running')
          )
        """,
        (agent_id,),
    ).fetchone()
    return int(row["c"] or 0) > 0


def dispatch_one_task_row(con, t) -> bool:
    openclaw_enabled = _env_bool("OPENCLAW_ENABLED", True)
    if not openclaw_enabled:
        con.execute(
            "UPDATE tasks SET last_error=?, updated_at=? WHERE id=?",
            ("openclaw_disabled", _utcnow_iso(), t["id"]),
        )
        _log("openclaw_dispatch_skipped", "task", str(t["id"]), "openclaw_disabled")
        return False

    if t["status"] != "approved":
        return False

    agent_id = t["assigned_agent_id"]
    if not agent_id:
        con.execute(
            "UPDATE tasks SET last_error=?, updated_at=? WHERE id=?",
            ("missing_assigned_agent", _utcnow_iso(), t["id"]),
        )
        _log("openclaw_dispatch_skipped", "task", str(t["id"]), "missing_assigned_agent")
        return False

    agent = con.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone()
    if not agent:
        con.execute(
            "UPDATE tasks SET last_error=?, updated_at=? WHERE id=?",
            ("agent_not_found", _utcnow_iso(), t["id"]),
        )
        _log("openclaw_dispatch_skipped", "task", str(t["id"]), "agent_not_found")
        return False

    payload_agent = {
        "id": str(agent["id"]),
        "name": agent["name"],
        "role": agent["role"] or "worker",
        "model": agent["model"] or "",
    }

    res = dispatch_job(
        title=t["title"],
        description=t["description"] or "",
        agent=payload_agent,
        metadata={"source": "zeroclaw", "task_id": int(t["id"]), "critical": int(t["is_critical"] or 0)},
    )

    if not res.get("ok"):
        err = res.get("error") or "dispatch_failed"
        con.execute(
            "UPDATE tasks SET last_error=?, updated_at=? WHERE id=?",
            (err, _utcnow_iso(), t["id"]),
        )
        _log("openclaw_dispatch_error", "task", str(t["id"]), err)
        return False

    job_id = res.get("job_id")
    con.execute(
        "UPDATE tasks SET status='active', openclaw_job_id=?, openclaw_job_status='queued', last_error=NULL, updated_at=? WHERE id=?",
        (job_id, _utcnow_iso(), t["id"]),
    )
    _log("openclaw_dispatched", "task", str(t["id"]), f"job_id={job_id} agent_id={agent_id}")
    return True


def _choose_reviewer_agent_id(con, preferred_id) -> Optional[int]:
    if preferred_id:
        try:
            return int(preferred_id)
        except Exception:
            return None

    row = con.execute(
        """
        SELECT id FROM agents
        WHERE is_active=1
          AND (
            lower(name) LIKE '%critic%'
            OR lower(role) LIKE '%critic%'
            OR lower(role) LIKE '%review%'
          )
        ORDER BY id ASC
        LIMIT 1
        """
    ).fetchone()
    if row:
        return int(row["id"])

    row = con.execute("SELECT id FROM agents WHERE is_active=1 ORDER BY id ASC LIMIT 1").fetchone()
    return int(row["id"]) if row else None


def _ensure_review_task_for(con, *, source_task_row: dict, reviewer_agent_id: Optional[int]) -> bool:
    src_id = int(source_task_row["id"])
    marker = f"[review_of_task_id:{src_id}]"

    # Avoid review-of-review loops
    if marker in (source_task_row.get("description") or ""):
        return False

    existing = con.execute(
        "SELECT id, status, openclaw_job_status FROM tasks WHERE description LIKE ? ORDER BY id DESC LIMIT 1",
        (f"%{marker}%",),
    ).fetchone()
    if existing:
        ex = dict(existing)
        # Allow re-creating if the existing review task is done but the job failed
        if not (ex.get("status") == "done" and ex.get("openclaw_job_status") == "failed"):
            return False
        # Otherwise fall through and create a new review task

    title = f"Review: Task #{src_id} — {source_task_row.get('title','')}"
    desc = (
        f"{marker}\n\n"
        f"You are a reviewer. Review the deliverable for Task #{src_id}.\n"
        f"Produce: (1) summary, (2) issues/risks, (3) concrete fixes/next tasks, (4) PASS/FAIL recommendation.\n\n"
        f"## Source Task Title\n{source_task_row.get('title','')}\n\n"
        f"## Source Task Description\n{(source_task_row.get('description') or '').strip()}\n\n"
        f"## Source Task Last Result\n{(source_task_row.get('last_result') or '').strip()}\n"
    )

    con.execute(
        """
        INSERT INTO tasks(title, description, status, assigned_agent_id, is_critical, requires_approval, created_at, updated_at)
        VALUES(?, ?, 'approved', ?, 0, 0, ?, ?)
        """,
        (title, desc, reviewer_agent_id, _utcnow_iso(), _utcnow_iso()),
    )
    new_id = con.execute("SELECT last_insert_rowid() AS x").fetchone()["x"]
    _log("review_task_created", "task", str(new_id), f"source_task_id={src_id}")
    return True


def tick_routines() -> None:
    ensure_default_routine()

    con = connect()
    routines = con.execute("SELECT * FROM routines WHERE is_enabled=1 ORDER BY updated_at DESC").fetchall()
    if not routines:
        con.close()
        return

    for r in routines:
        r = dict(r)
        kind = (r.get("kind") or "").strip()

        # (A) Create review-tasks for items in Dev Done
        if kind == "review_autocreate":
            reviewer_id = _choose_reviewer_agent_id(con, r.get("agent_id"))
            review_rows = con.execute(
                "SELECT * FROM tasks WHERE status IN ('dev_done','review') ORDER BY updated_at ASC, id ASC LIMIT 50"
            ).fetchall()

            created_any = False
            for src in review_rows:
                if _ensure_review_task_for(con, source_task_row=dict(src), reviewer_agent_id=reviewer_id):
                    created_any = True

            if created_any:
                con.commit()
            continue

        # (C) Resolve blocked tasks via architect
        if kind == 'blocked_resolution':
            _tick_blocked_resolution(con, r)
            continue

        # (D) Plan next phase when all tasks done/blocked
        if kind == 'planning_next_phase':
            _tick_planning_next_phase(con, r, _utcnow_iso())
            continue

        # (B) Full workflow automation: idle_autostart
        if kind and kind != "idle_autostart":
            continue

        agent_id = r.get("agent_id")
        claim = int(r.get("claim_unassigned") or 0) == 1

        if agent_id:
            agent_ids = [int(agent_id)]
        else:
            agent_ids = [int(a["id"]) for a in con.execute("SELECT id FROM agents WHERE is_active=1 ORDER BY id").fetchall()]

        # ── Step 0: Reset stale running jobs (stuck > 10 min) ──
        stale_running = con.execute(
            "SELECT * FROM tasks WHERE status IN ('active','running') "
            "AND openclaw_job_status='running' "
            "AND updated_at < datetime('now', '-10 minutes')"
        ).fetchall()
        for sr in stale_running:
            sr = dict(sr)
            con.execute(
                "UPDATE tasks SET status='approved', openclaw_job_id=NULL, "
                "openclaw_job_status=NULL, last_error='stale_running_reset', "
                "updated_at=? WHERE id=?",
                (_utcnow_iso(), sr["id"])
            )
            _log("stale_job_reset", "task", sr["id"],
                 f"Task '{sr['title']}' was running >10min, reset to approved")
        if stale_running:
            con.commit()

        # ── Step 1: Move completed active tasks → dev_done ──
        completed_active = con.execute(
            "SELECT * FROM tasks WHERE status='active' AND openclaw_job_status='completed'"
        ).fetchall()
        for ct in completed_active:
            ct = dict(ct)
            con.execute(
                "UPDATE tasks SET status='dev_done', updated_at=? WHERE id=?",
                (_utcnow_iso(), ct["id"]),
            )
            _log("workflow_advance", "task", str(ct["id"]), "active→dev_done (job completed)")
        if completed_active:
            con.commit()

        # ── Step 2: Re-dispatch failed/blocked tasks (max 3 retries) ──
        MAX_RETRIES = 3
        failed_tasks = con.execute(
            "SELECT * FROM tasks WHERE "
            "((status='active' AND openclaw_job_status='failed') OR "
            " (status='blocked')) "
            "AND retry_count < ?",
            (MAX_RETRIES,)
        ).fetchall()
        for ft in failed_tasks:
            ft = dict(ft)
            new_retry = ft.get("retry_count", 0) + 1
            con.execute(
                "UPDATE tasks SET status='approved', openclaw_job_id=NULL, openclaw_job_status=NULL, "
                "last_error=NULL, retry_count=?, updated_at=? WHERE id=?",
                (new_retry, _utcnow_iso(), ft["id"]),
            )
            _log("workflow_retry", "task", str(ft["id"]),
                 f"{ft['status']}→approved retry {new_retry}/{MAX_RETRIES}")
        if failed_tasks:
            con.commit()

        # Mark tasks that exceeded retries as permanently blocked
        exhausted = con.execute(
            "SELECT * FROM tasks WHERE "
            "((status='active' AND openclaw_job_status='failed') OR "
            " (status='blocked' AND last_error!='max_retries_exceeded')) "
            "AND retry_count >= ?",
            (MAX_RETRIES,)
        ).fetchall()
        for et in exhausted:
            et = dict(et)
            con.execute(
                "UPDATE tasks SET status='blocked', last_error='max_retries_exceeded', updated_at=? WHERE id=?",
                (_utcnow_iso(), et["id"]),
            )
            _log("workflow_max_retries", "task", str(et["id"]),
                 f"exceeded {MAX_RETRIES} retries, permanently blocked")
        if exhausted:
            con.commit()

        # ── Step 2b: Clean stale openclaw fields on pending/approved tasks ──
        stale = con.execute(
            "SELECT * FROM tasks WHERE status IN ('pending','approved') AND openclaw_job_status IS NOT NULL AND openclaw_job_status!=''"
        ).fetchall()
        for st in stale:
            st = dict(st)
            con.execute(
                "UPDATE tasks SET openclaw_job_id=NULL, openclaw_job_status=NULL, last_error=NULL, updated_at=? WHERE id=?",
                (_utcnow_iso(), st["id"]),
            )
            _log("workflow_cleanup", "task", str(st["id"]),
                 f"cleared stale openclaw fields (was {st.get('openclaw_job_status')})")
        if stale:
            con.commit()

        # ── Step 3: Auto-approve non-critical pending tasks ──
        pending_noncrit = con.execute(
            "SELECT * FROM tasks WHERE status='pending' AND is_critical=0"
        ).fetchall()
        for pt in pending_noncrit:
            pt = dict(pt)
            con.execute(
                "UPDATE tasks SET status='approved', updated_at=? WHERE id=?",
                (_utcnow_iso(), pt["id"]),
            )
            _log("workflow_auto_approve", "task", str(pt["id"]), "pending→approved (non-critical)")
        if pending_noncrit:
            con.commit()

        # ── Step 4: Claim unassigned approved tasks ──
        if claim:
            unassigned = con.execute(
                "SELECT * FROM tasks WHERE status='approved' AND assigned_agent_id IS NULL ORDER BY updated_at ASC, id ASC"
            ).fetchall()
            for ut in unassigned:
                ut = dict(ut)
                # Find first idle agent
                for aid in agent_ids:
                    if not agent_is_running(con, aid):
                        con.execute(
                            "UPDATE tasks SET assigned_agent_id=?, updated_at=? WHERE id=?",
                            (aid, _utcnow_iso(), ut["id"]),
                        )
                        _log("workflow_claim", "task", str(ut["id"]), f"assigned to agent {aid}")
                        con.commit()
                        break

        # ── Step 5: Dispatch approved tasks to idle agents ──
        for aid in agent_ids:
            if agent_is_running(con, aid):
                continue

            t = con.execute(
                "SELECT * FROM tasks WHERE status='approved' AND (openclaw_job_id IS NULL OR openclaw_job_id='') AND assigned_agent_id=? ORDER BY updated_at ASC, id ASC LIMIT 1",
                (aid,),
            ).fetchone()

            if not t:
                continue

            if dispatch_one_task_row(con, t):
                con.commit()

    con.close()


@router.get("/routines", response_class=HTMLResponse)
def routines_page(request: Request) -> HTMLResponse:
    ensure_default_routine()
    con = connect()
    routines = con.execute(
        "SELECT r.*, a.name AS agent_name FROM routines r LEFT JOIN agents a ON a.id=r.agent_id ORDER BY r.updated_at DESC"
    ).fetchall()
    agents = con.execute("SELECT id,name FROM agents ORDER BY id").fetchall()
    con.close()
    return templates.TemplateResponse(
        "routines.html",
        {"request": request, "routines": routines, "agents": agents},
    )


def _parse_routine_prompt(prompt: str) -> dict:
    """Parse a natural-language prompt into routine config."""
    p = prompt.lower()
    config = {
        "name": prompt.strip()[:80],
        "description": prompt.strip(),
        "kind": "idle_autostart",
        "claim_unassigned": 0,
        "agent_id": None,
    }
    # Detect kind
    if any(kw in p for kw in ['plan', 'next phase', 'next development', 'all done', 'all complete', 'new set of tasks', 'next sprint']):
        config['kind'] = 'planning_next_phase'
        if 'plan' in p:
            config['name'] = 'Plan next development phase when all tasks complete'
    elif any(kw in p for kw in ['blocked', 'resolve', 'unblock', 'diagnose', 'fix block']):
        config['kind'] = 'blocked_resolution'
        if 'blocked' in p or 'resolve' in p:
            config['name'] = 'Resolve blocked tasks via architect'
    elif any(kw in p for kw in ['review', 'critique', 'feedback', 'inspect', 'dev done', 'dev_done']):
        config["kind"] = "review_autocreate"
        if "review" in p and "create" not in config["name"].lower():
            config["name"] = "Auto-create review tasks"
    elif any(kw in p for kw in ["email", "report", "summary", "notify", "status report"]):
        config["kind"] = "status_report_email"
        if "email" in p or "report" in p:
            config["name"] = "Status report email"
    else:
        config["kind"] = "idle_autostart"
    # Detect claim
    if any(kw in p for kw in ["claim", "unassigned", "assign idle", "pick up", "grab"]):
        config["claim_unassigned"] = 1
    # Detect agent scope
    import re as _re
    agent_match = _re.search(r"agent[\s#]*(\d+)", p)
    if agent_match:
        config["agent_id"] = int(agent_match.group(1))
    for name_kw, aid in [("ada", 1), ("jorven", 2), ("iris", 3), ("quimby", 4)]:
        if name_kw in p:
            config["agent_id"] = aid
            break
    return config

@router.post("/routines/create")
def routines_create(
    prompt: str = Form(...),
):
    ensure_default_routine()
    cfg = _parse_routine_prompt(prompt)
    rid = secrets.token_hex(8)
    con = connect()
    con.execute(
        "INSERT INTO routines(id,name,kind,is_enabled,agent_id,claim_unassigned,description,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (
            rid,
            cfg["name"],
            cfg["kind"],
            1,
            cfg["agent_id"],
            cfg["claim_unassigned"],
            cfg["description"],
            _utcnow_iso(),
            _utcnow_iso(),
        ),
    )
    con.commit()
    con.close()
    _log("routine_created", "routine", rid, cfg["name"])
    return RedirectResponse(url="/routines", status_code=303)


@router.post("/routines/{routine_id}/toggle")
def routines_toggle(routine_id: str):
    ensure_default_routine()
    con = connect()
    row = con.execute("SELECT is_enabled FROM routines WHERE id=?", (routine_id,)).fetchone()
    if not row:
        con.close()
        return JSONResponse({"ok": False, "error": "not_found"}, status_code=404)
    newv = 0 if int(row["is_enabled"] or 0) == 1 else 1
    con.execute("UPDATE routines SET is_enabled=?, updated_at=? WHERE id=?", (newv, _utcnow_iso(), routine_id))
    con.commit()
    con.close()
    _log("routine_toggled", "routine", routine_id, f"is_enabled={newv}")
    return RedirectResponse(url="/routines", status_code=303)


@router.post("/routines/{routine_id}/update")
def routines_update(
    routine_id: str,
    prompt: str = Form(...),
):
    ensure_default_routine()
    cfg = _parse_routine_prompt(prompt)
    con = connect()
    con.execute(
        "UPDATE routines SET name=?, kind=?, agent_id=?, claim_unassigned=?, description=?, updated_at=? WHERE id=?",
        (
            cfg["name"],
            cfg["kind"],
            cfg["agent_id"],
            cfg["claim_unassigned"],
            cfg["description"],
            _utcnow_iso(),
            routine_id,
        ),
    )
    con.commit()
    con.close()
    _log("routine_updated", "routine", routine_id, cfg["name"])
    return RedirectResponse(url="/routines", status_code=303)


@router.post("/routines/{routine_id}/delete")
def routines_delete(routine_id: str):
    ensure_default_routine()
    con = connect()
    con.execute("DELETE FROM routines WHERE id=?", (routine_id,))
    con.commit()
    con.close()
    _log("routine_deleted", "routine", routine_id, "")
    return RedirectResponse(url="/routines", status_code=303)


def _tick_status_report_email(con, r: dict) -> None:
    """Send a summary report email when enough tasks completed since last report.

    Rules:
    - at least 10 qualifying tasks reached done since last report
    - minimum 30 minutes between reports
    - exclude review tasks unless critical/important
    """
    from datetime import datetime, timezone, timedelta
    from .emailer import send_email

    now = datetime.now(timezone.utc).replace(microsecond=0)

    # state storage (simple kv table)
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS routine_state(
          routine_id TEXT,
          key TEXT,
          value TEXT,
          updated_at TEXT,
          PRIMARY KEY(routine_id, key)
        )
        """
    )

    def get_state(key: str, default: str = "") -> str:
        row = con.execute(
            "SELECT value FROM routine_state WHERE routine_id=? AND key=?",
            (r["id"], key),
        ).fetchone()
        return (row["value"] if row else default) or default

    def set_state(key: str, value: str) -> None:
        con.execute(
            "INSERT OR REPLACE INTO routine_state(routine_id,key,value,updated_at) VALUES(?,?,?,?)",
            (r["id"], key, value, _utcnow_iso()),
        )

    last_sent = get_state("last_sent_at", "")
    last_done_id = int(get_state("last_done_id", "0") or 0)

    if last_sent:
        try:
            dt = datetime.fromisoformat(last_sent)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if now - dt < timedelta(minutes=30):
                return
        except Exception:
            pass

    # Qualifying done tasks since last_done_id
    rows = con.execute(
        """
        SELECT id,title,description,is_critical,updated_at,last_result
        FROM tasks
        WHERE status='done' AND id > ?
        ORDER BY id ASC
        LIMIT 2000
        """,
        (last_done_id,),
    ).fetchall()

    def is_review_task_row(row) -> bool:
        title = str(row["title"] or "").lower().strip()
        desc = str(row["description"] or "").lower()
        return title.startswith('review:') or ('[review_of_task_id:' in desc)

    def is_important(row) -> bool:
        if int(row["is_critical"] or 0) == 1:
            return True
        blob = (str(row["title"] or "") + "\n" + str(row["description"] or "") + "\n" + str(row["last_result"] or "")).lower()
        for kw in ("critical","important","blocker","security","vulnerability","risk","exploit"):
            if kw in blob:
                return True
        return False

    qualifying = []
    max_id = last_done_id
    for row in rows:
        max_id = max(max_id, int(row["id"]))
        if is_review_task_row(row) and not is_important(row):
            continue
        qualifying.append(row)

    if len(qualifying) < 10:
        # Still advance last_done_id? no, we don't want to skip. Leave it.
        return

    # Build report
    items_html = "".join(
        [
            f"<li><b>#{int(t['id'])}</b> {t['title']}<br><pre style='white-space:pre-wrap'>{(t['last_result'] or '')[:2000]}</pre></li>"
            for t in qualifying[-20:]
        ]
    )
    html = f"""
    <h2>ZeroClaw status report</h2>
    <p>Completed qualifying tasks since last report: <b>{len(qualifying)}</b></p>
    <p>Showing last 20:</p>
    <ol>{items_html}</ol>
    """

    to_addr = os.getenv("STATUS_REPORT_EMAIL_TO") or os.getenv("APPROVER_EMAIL") or os.getenv("EMAIL_TO") or ""
    if not to_addr:
        _log("status_report_email_skipped", "routine", r["id"], "no_to_addr")
        return

    try:
        send_email(to_addr=to_addr, subject="[ZeroClaw] Summary report", html_body=html)
        set_state("last_sent_at", now.isoformat())
        set_state("last_done_id", str(max_id))
        con.commit()
        _log("status_report_email_sent", "routine", r["id"], f"count={len(qualifying)} max_done_id={max_id}")
    except Exception as e:
        _log("status_report_email_error", "routine", r["id"], str(e))





def _tick_blocked_resolution(con, r: dict) -> None:
    """Dispatch blocked tasks to the architect agent for resolution analysis."""

    # Find architect agent
    architect = con.execute(
        "SELECT id FROM agents WHERE role='architect' AND is_active=1 LIMIT 1"
    ).fetchone()
    if not architect:
        architect = con.execute("SELECT id FROM agents WHERE id=2 AND is_active=1 LIMIT 1").fetchone()
    if not architect:
        return
    architect_id = architect["id"]

    # Note: we create the resolve task as 'approved' regardless of architect availability.
    # The idle_autostart routine will dispatch it when the architect is free.

    # Find blocked tasks that need resolution
    blocked = con.execute(
        """SELECT * FROM tasks
           WHERE status='blocked'
             AND title NOT LIKE 'Resolve:%'
             AND (last_error IS NOT NULL AND last_error != '')
           ORDER BY updated_at ASC LIMIT 1"""
    ).fetchall()

    for bt in blocked:
        bt = dict(bt)
        task_id = bt["id"]

        # Check if a resolution task already exists
        existing = con.execute(
            "SELECT id FROM tasks WHERE title LIKE ? AND status NOT IN ('done') LIMIT 1",
            (f"Resolve: Task #{task_id}%",)
        ).fetchone()
        if existing:
            continue

        # Create a resolution task assigned to the architect
        title = f"Resolve: Task #{task_id} \u2014 {bt.get('title','')}"
        desc_parts = [
            f"[resolve_blocked_task_id:{task_id}]",
            "",
            "You are the architect. A task is blocked and needs your analysis.",
            "Analyze the error, propose a fix or workaround, and provide updated instructions.",
            "",
            "## Blocked Task",
            f"**Title:** {bt.get('title','')}" ,
            f"**Description:** {(bt.get('description') or '').strip()}",
            "",
            "## Error Details",
            str(bt.get("last_error") or "unknown"),
            "",
            "## Last Result",
            str(bt.get("last_result") or "none"),
            "",
            "## Your Task",
            "1. Diagnose the root cause of the block/failure",
            "2. Propose specific fixes or workarounds",
            "3. Provide updated task instructions that would prevent this error",
            "4. If the task should be abandoned, explain why",
        ]
        desc = chr(10).join(desc_parts)

        con.execute(
            """INSERT INTO tasks(title, description, status, assigned_agent_id,
               is_critical, requires_approval, created_at, updated_at)
               VALUES(?, ?, 'approved', ?, 0, 0, ?, ?)""",
            (title, desc, architect_id, _utcnow_iso(), _utcnow_iso()),
        )
        con.commit()
        _log("blocked_resolution_created", "task", str(task_id),
             f"resolution_task_created assigned_to_architect={architect_id}")

    # Handle completed resolution tasks: unblock the source task
    import re as _re3
    resolved = con.execute(
        """SELECT * FROM tasks
           WHERE title LIKE 'Resolve:%' AND status='done'
             AND description LIKE '%[resolve_blocked_task_id:%'"""
    ).fetchall()

    for rt in resolved:
        rt = dict(rt)
        m = _re3.search(r"\[resolve_blocked_task_id:(\d+)\]", rt.get("description", ""))
        if not m:
            continue
        src_id = int(m.group(1))
        src = con.execute("SELECT id, status FROM tasks WHERE id=?", (src_id,)).fetchone()
        if src and dict(src)["status"] == "blocked":
            resolution = rt.get("last_result") or ""
            con.execute(
                "UPDATE tasks SET status='approved', review_summary=?, last_error=NULL, retry_count=0, updated_at=? WHERE id=?",
                (resolution, _utcnow_iso(), src_id)
            )
            con.commit()
            _log("blocked_task_unblocked", "task", str(src_id),
                 f"unblocked_via_resolution_task={rt['id']}")

        # Delete the resolution task after processing
        con.execute("DELETE FROM decisions WHERE task_id=?", (rt["id"],))
        con.execute("DELETE FROM critiques WHERE task_id=?", (rt["id"],))
        con.execute("DELETE FROM tasks WHERE id=?", (rt["id"],))
        con.commit()
        _log("resolution_task_deleted", "task", str(rt["id"]),
             f"resolution_applied_to_task={src_id}")


def _tick_planning_next_phase(con, routine, now_iso):
    """When all non-review, non-resolve tasks are done or blocked, create architect planning task."""
    # Count tasks that are NOT done/blocked and NOT review/resolve helper tasks
    active_tasks = con.execute("""
        SELECT COUNT(*) FROM tasks
        WHERE status NOT IN ('done', 'blocked')
          AND title NOT LIKE 'Review:%'
          AND title NOT LIKE 'Resolve:%'
          AND title NOT LIKE 'Plan:%'
    """).fetchone()[0]

    if active_tasks > 0:
        return  # Still work in progress

    # Check we don't already have a pending/active planning task
    existing_plan = con.execute("""
        SELECT COUNT(*) FROM tasks
        WHERE title LIKE 'Plan:%'
          AND status NOT IN ('done', 'blocked')
    """).fetchone()[0]

    if existing_plan > 0:
        return  # Already have a planning task in progress

    # Gather context: done tasks, blocked tasks with errors
    done_tasks = con.execute("""
        SELECT id, title, review_summary FROM tasks
        WHERE status = 'done'
          AND title NOT LIKE 'Review:%'
          AND title NOT LIKE 'Resolve:%'
          AND title NOT LIKE 'Plan:%'
        ORDER BY updated_at DESC LIMIT 20
    """).fetchall()

    blocked_tasks = con.execute("""
        SELECT id, title, last_error, last_result FROM tasks
        WHERE status = 'blocked'
          AND title NOT LIKE 'Review:%'
          AND title NOT LIKE 'Resolve:%'
          AND title NOT LIKE 'Plan:%'
        ORDER BY updated_at DESC LIMIT 10
    """).fetchall()

    done_summary = "\n".join([f"- [DONE] #{dict(t)['id']}: {dict(t)['title']}" + (f" (Review: {dict(t)['review_summary'][:200]}...)" if dict(t).get('review_summary') else "") for t in done_tasks]) or "(none)"
    blocked_summary = "\n".join([f"- [BLOCKED] #{dict(t)['id']}: {dict(t)['title']} — Error: {dict(t).get('last_error','')}" for t in blocked_tasks]) or "(none)"

    total_done = con.execute("SELECT COUNT(*) FROM tasks WHERE status='done' AND title NOT LIKE 'Review:%' AND title NOT LIKE 'Resolve:%' AND title NOT LIKE 'Plan:%'").fetchone()[0]
    total_blocked = con.execute("SELECT COUNT(*) FROM tasks WHERE status='blocked' AND title NOT LIKE 'Review:%' AND title NOT LIKE 'Resolve:%' AND title NOT LIKE 'Plan:%'").fetchone()[0]

    description = f"""All current tasks have reached completion or are blocked. Plan the next development phase.

COMPLETED TASKS ({total_done}):
{done_summary}

BLOCKED TASKS ({total_blocked}):
{blocked_summary}

INSTRUCTIONS:
Based on the completed work, blocked items, the project roadmap, and milestones:
1. Identify what blockers need to be resolved
2. Determine the next logical development tasks
3. Create a prioritized list of 3-8 new tasks with clear titles and descriptions
4. Each task should advance the 2D medieval game project
5. Consider dependencies between tasks
6. Output the task list as JSON: {{"tasks": [{{"title": "...", "description": "...", "is_critical": 0, "suggested_agent": "architect|programmer|reviewer|reporter"}}]}}

[planning_phase_task]"""

    # Assign to architect (agent_id=2)
    con.execute(
        """INSERT INTO tasks(title, description, status, assigned_agent_id, is_critical, created_at, updated_at,
           schedule_type, cron_expr, interval_minutes, next_run_at, is_recurring, retry_count)
           VALUES(?,?,?,?,?,?,?, 'none',NULL,NULL,NULL,0,0)""",
        (f"Plan: Next Development Phase", description, 'pending', 2, 1, now_iso, now_iso)
    )
    con.execute(
        'INSERT INTO action_logs(ts, action, entity_type, entity_id, detail, layer, model) VALUES(?,?,?,?,?,?,?)',
        (now_iso, 'planning_next_phase_created', 'task', 'new', f'done={total_done} blocked={total_blocked}', 'zeroclaw', None)
    )
    con.commit()
