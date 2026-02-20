from __future__ import annotations

import os
from typing import Any

import httpx


def _base_url() -> str:
    return (os.getenv("OPENCLAW_BASE_URL") or "").rstrip("/")


def _auth_header() -> dict[str, str]:
    tok = os.getenv("OPENCLAW_AUTH_TOKEN") or os.getenv("OPENCLAW_TOKEN") or ""
    if not tok:
        return {}
    if tok.lower().startswith("bearer "):
        return {"Authorization": tok}
    return {"Authorization": f"Bearer {tok}"}


def dispatch_job(*, title: str, description: str, agent: dict[str, Any] | None, metadata: dict[str, Any]) -> dict[str, Any]:
    base = _base_url()
    if not base:
        return {"ok": False, "error": "openclaw_base_url_missing"}

    openrouter_key = os.getenv("OPENROUTER_API_KEY") or ""
    if not openrouter_key:
        return {"ok": False, "error": "openrouter_api_key_missing"}

    url = f"{base}/jobs"
    payload = {
        "task": {"title": title, "description": description},
        "agent": agent or {},
        "openrouter_api_key": openrouter_key,
        "metadata": metadata or {},
    }

    try:
        with httpx.Client(timeout=30.0) as client:
            r = client.post(url, json=payload, headers={**_auth_header()})
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        return {"ok": False, "error": f"dispatch_failed: {e}"}

    job_id = data.get("job_id") or data.get("id") or data.get("jobId")
    if not job_id:
        return {"ok": False, "error": "dispatch_no_job_id", "raw": data}

    return {"ok": True, "job_id": str(job_id), "raw": data}


def get_status(*, job_id: str) -> dict[str, Any]:
    base = _base_url()
    if not base:
        return {"ok": False, "error": "openclaw_base_url_missing"}

    url = f"{base}/status/{job_id}"
    try:
        with httpx.Client(timeout=30.0) as client:
            r = client.get(url, headers={**_auth_header()})
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        return {"ok": False, "error": f"status_failed: {e}"}

    return {"ok": True, "raw": data}


def normalize_state(payload: dict[str, Any]) -> str:
    st = (payload or {}).get("status") or (payload or {}).get("state") or ""
    st = str(st).lower().strip()
    if st in ("queued", "pending"):
        return "queued"
    if st in ("running", "in_progress", "inprogress"):
        return "running"
    if st in ("completed", "complete", "succeeded", "success", "done"):
        return "completed"
    if st in ("failed", "error", "cancelled", "canceled"):
        return "failed"
    return st or "unknown"
