from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone

from .db import connect


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _pepper() -> bytes:
    # Prefer ZEROCLAW_TOKEN_PEPPER, fall back to APPROVAL_SECRET (from uploaded .env)
    return (os.getenv("ZEROCLAW_TOKEN_PEPPER") or os.getenv("APPROVAL_SECRET") or "dev-pepper-change-me").encode("utf-8")


def _hash_token(token: str, salt_b64: str) -> str:
    salt = base64.urlsafe_b64decode(salt_b64.encode("ascii"))
    key = _pepper() + salt
    return hmac.new(key, token.encode("utf-8"), hashlib.sha256).hexdigest()


def create_decision(*, entity_type: str, entity_id: int, action: str, requester: str, ttl_hours: int, result_markdown: str) -> tuple[str, str]:
    decision_id = secrets.token_urlsafe(16)
    token = secrets.token_urlsafe(24)
    salt = secrets.token_bytes(16)
    salt_b64 = base64.urlsafe_b64encode(salt).decode("ascii")
    token_hash = _hash_token(token, salt_b64)

    expires_at = (datetime.now(timezone.utc) + timedelta(hours=ttl_hours)).replace(microsecond=0).isoformat()

    con = connect()
    con.execute(
        """
        INSERT INTO decisions(decision_id, entity_type, entity_id, action, status, token_hash, token_salt, expires_at, requested_at, requester, result_markdown)
        VALUES(?,?,?,?, 'pending', ?,?,?, ?,?,?)
        """,
        (decision_id, entity_type, entity_id, action, token_hash, salt_b64, expires_at, utcnow_iso(), requester, result_markdown),
    )
    con.execute(
        "INSERT INTO action_logs(ts, action, entity_type, entity_id, detail) VALUES(?,?,?,?,?)",
        (utcnow_iso(), "decision_created", entity_type, str(entity_id), f"decision_id={decision_id}"),
    )
    con.commit()
    con.close()
    return decision_id, token


def verify_decision_token(*, decision_id: str, token: str):
    con = connect()
    row = con.execute("SELECT * FROM decisions WHERE decision_id=?", (decision_id,)).fetchone()
    con.close()
    if not row:
        return None

    if row["expires_at"]:
        try:
            exp = datetime.fromisoformat(row["expires_at"])
            if datetime.now(timezone.utc) > exp:
                return None
        except Exception:
            return None

    expected = _hash_token(token, row["token_salt"])
    if not hmac.compare_digest(expected, row["token_hash"]):
        return None
    return dict(row)


def apply_decision(*, decision_id: str, approve: bool, decider_ip: str | None, decider_ua: str | None) -> dict | None:
    con = connect()
    row = con.execute("SELECT * FROM decisions WHERE decision_id=?", (decision_id,)).fetchone()
    if not row:
        con.close()
        return None
    if row["status"] != "pending":
        con.close()
        return dict(row)

    new_status = "approved" if approve else "rejected"

    con.execute(
        "UPDATE decisions SET status=?, decided_at=?, decider_ip=?, decider_ua=? WHERE decision_id=?",
        (new_status, utcnow_iso(), decider_ip, decider_ua, decision_id),
    )

    if row["entity_type"] == "task":
        task_status = "approved" if approve else "rejected"
        con.execute("UPDATE tasks SET status=?, updated_at=? WHERE id=?", (task_status, utcnow_iso(), row["entity_id"]))

    con.execute(
        "INSERT INTO action_logs(ts, action, entity_type, entity_id, detail) VALUES(?,?,?,?,?)",
        (utcnow_iso(), f"decision_{new_status}", row["entity_type"], str(row["entity_id"]), f"decision_id={decision_id}"),
    )

    con.commit()
    out = con.execute("SELECT * FROM decisions WHERE decision_id=?", (decision_id,)).fetchone()
    con.close()
    return dict(out) if out else None
