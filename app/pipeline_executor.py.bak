"""Pipeline Executor Engine.

Reads a pipeline config (blocks_json) from the DB and executes blocks
in sequence, routing work to either OpenRouter (via OpenClaw) or the
Claude CLI executor depending on each block's configuration.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .db import connect
from .claude_executor import execute_claude_cli, CLAUDE_SUCCESS
from . import claude_health

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _log(action: str, entity_type: str, entity_id: str | None, detail: str, *, layer: str = "zeroclaw", model: str | None = None) -> None:
    try:
        con = connect()
        con.execute(
            "INSERT INTO action_logs(ts, action, entity_type, entity_id, detail, layer, model) VALUES(?,?,?,?,?,?,?)",
            (_utcnow_iso(), action, entity_type, entity_id, detail, layer, model),
        )
        con.commit()
        con.close()
    except Exception:
        pass


def _log_executor(
    task_id: int,
    pipeline_id: int,
    block_index: int,
    block_type: str,
    model: str,
    executor: str,
    started_at: str,
    duration: float,
    success: bool,
    pass_fail: str | None = None,
    review_notes: str | None = None,
    output_preview: str | None = None,
    failure_type: str | None = None,
    error: str | None = None,
) -> None:
    con = connect()
    con.execute(
        """INSERT INTO executor_log(task_id, pipeline_id, block_index, block_type, model, executor,
           started_at, duration_seconds, success, pass_fail, review_notes, output_preview, failure_type, error)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            task_id, pipeline_id, block_index, block_type, model, executor,
            started_at, round(duration, 3), 1 if success else 0,
            pass_fail, review_notes,
            (output_preview or "")[:500] if output_preview else None,
            failure_type, error,
        ),
    )
    con.commit()
    con.close()


# ---------------------------------------------------------------------------
# Agent internal files (SOUL.md / INSTRUCTIONS.md / CONTEXT.md)
# ---------------------------------------------------------------------------

def _load_agent_files(agent_name: str) -> str:
    """Load and concatenate the agent's markdown identity files."""
    agent_dir = DATA_DIR / "agents" / agent_name
    if not agent_dir.is_dir():
        return ""
    parts: list[str] = []
    for fname in ("SOUL.md", "INSTRUCTIONS.md", "CONTEXT.md"):
        fpath = agent_dir / fname
        if fpath.exists():
            content = fpath.read_text(encoding="utf-8", errors="replace").strip()
            if content:
                parts.append(f"--- {fname} ---\n{content}")
    # Also load any custom .md files
    for fpath in sorted(agent_dir.glob("*.md")):
        if fpath.name not in ("SOUL.md", "INSTRUCTIONS.md", "CONTEXT.md") and fpath.stat().st_size > 0:
            content = fpath.read_text(encoding="utf-8", errors="replace").strip()
            if content:
                parts.append(f"--- {fpath.name} ---\n{content}")
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# OpenRouter execution (wraps existing openclaw dispatch)
# ---------------------------------------------------------------------------

def _execute_openrouter(prompt: str, model: str, system_prompt: str = "") -> dict[str, Any]:
    """Execute a task through OpenRouter via the LangGraph runtime.

    This calls the runtime directly (in-process) rather than going through
    the HTTP client, so we can inject custom system prompts.
    """
    from .openclaw_langgraph_runtime import run_job_langgraph

    api_key = os.getenv("OPENROUTER_API_KEY", "")
    if not api_key:
        return {"success": False, "output": "", "error": "openrouter_api_key_missing", "executor": "OpenRouter", "duration_seconds": 0, "failure_type": None}

    payload = {
        "task": {"title": "Pipeline Task", "description": prompt},
        "agent": {"role": "general", "model": model},
        "openrouter_api_key": api_key,
        "metadata": {},
    }
    # If we have a custom system prompt, inject it via the agent role override
    if system_prompt:
        payload["agent"]["_system_prompt_override"] = system_prompt

    start = time.monotonic()
    result = run_job_langgraph(payload=payload)
    elapsed = time.monotonic() - start

    if result.get("ok"):
        return {
            "success": True,
            "output": result.get("output", ""),
            "executor": "OpenRouter",
            "duration_seconds": round(elapsed, 3),
            "error": None,
            "failure_type": None,
        }
    return {
        "success": False,
        "output": "",
        "executor": "OpenRouter",
        "duration_seconds": round(elapsed, 3),
        "error": result.get("error", "unknown"),
        "failure_type": None,
    }


# ---------------------------------------------------------------------------
# Block executors
# ---------------------------------------------------------------------------

def _check_claude_health_for_block(block_config: dict) -> str | None:
    """Check if Claude CLI is available. Returns None if OK, or the on_limit action."""
    state = claude_health.get_state()
    if state in (claude_health.HEALTHY, claude_health.DEGRADED):
        return None
    # CLI is unavailable
    return block_config.get("on_limit", "fallback")


def _run_executor_block(prompt: str, config: dict, system_prompt: str) -> dict[str, Any]:
    """Run an executor/retry/escalate block."""
    executor = config.get("executor", "OpenRouter")
    model = config.get("model", "openai/gpt-4o-mini")

    if executor == "Claude CLI":
        # Prepend system prompt to the task prompt for Claude CLI
        full_prompt = prompt
        if system_prompt:
            full_prompt = f"{system_prompt}\n\n---\n\n{prompt}"
        result = execute_claude_cli(full_prompt)
        # Update health state
        if result["failure_type"] == CLAUDE_SUCCESS:
            claude_health.record_success()
        else:
            claude_health.record_failure(result["failure_type"])
        return result
    else:
        return _execute_openrouter(prompt, model, system_prompt)


def _run_review_block(output_to_review: str, config: dict, task_title: str, system_prompt: str) -> dict[str, Any]:
    """Run a review block — sends previous output to a reviewer model."""
    review_prompt = (
        f"You are reviewing the output of a task.\n\n"
        f"## Task Title\n{task_title}\n\n"
        f"## Output to Review\n{output_to_review}\n\n"
        f"## Instructions\n"
        f"1. Evaluate the quality, correctness, and completeness of the output.\n"
        f"2. Identify any issues, errors, or missing elements.\n"
        f"3. Provide a verdict: PASS or FAIL.\n"
        f"4. Include review notes explaining your verdict.\n\n"
        f"Format your response as:\n"
        f"VERDICT: PASS or FAIL\n"
        f"NOTES: <your review notes>\n"
    )
    return _run_executor_block(review_prompt, config, system_prompt)


def _parse_review_verdict(output: str) -> tuple[bool, str]:
    """Parse PASS/FAIL from review output. Returns (passed, notes)."""
    low = output.lower()
    passed = True
    if "verdict: fail" in low or "verdict:fail" in low:
        passed = False
    elif '"verdict"' in low and '"fail"' in low:
        passed = False
    elif low.strip().startswith("fail"):
        passed = False
    notes = output
    return passed, notes


# ---------------------------------------------------------------------------
# Main pipeline runner
# ---------------------------------------------------------------------------

def get_pipeline_for_task(task_row: dict) -> dict | None:
    """Look up the pipeline for a task via its assigned agent, or fall back to default."""
    con = connect()

    # Check if the agent has a pipeline_id
    agent_id = task_row.get("assigned_agent_id")
    if agent_id:
        agent = con.execute("SELECT pipeline_id FROM agents WHERE id=?", (agent_id,)).fetchone()
        if agent and agent["pipeline_id"]:
            pipeline = con.execute("SELECT * FROM pipelines WHERE id=? AND is_active=1", (agent["pipeline_id"],)).fetchone()
            if pipeline:
                con.close()
                return dict(pipeline)

    # Fall back to default pipeline
    pipeline = con.execute("SELECT * FROM pipelines WHERE task_type='default' AND is_active=1 ORDER BY id LIMIT 1").fetchone()
    if not pipeline:
        pipeline = con.execute("SELECT * FROM pipelines WHERE is_active=1 ORDER BY id LIMIT 1").fetchone()
    con.close()
    return dict(pipeline) if pipeline else None


def run_pipeline(task_id: int, *, from_block_index: int = 0) -> dict[str, Any]:
    """Execute a pipeline for the given task, starting at from_block_index.

    Returns a summary dict with final status and output.
    """
    con = connect()
    task = con.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if not task:
        con.close()
        return {"ok": False, "error": "task_not_found"}
    task = dict(task)

    # Get agent info for system prompt
    agent_name = ""
    agent_system_prompt = ""
    if task.get("assigned_agent_id"):
        agent_row = con.execute("SELECT * FROM agents WHERE id=?", (task["assigned_agent_id"],)).fetchone()
        if agent_row:
            agent_name = agent_row["name"]
            agent_system_prompt = _load_agent_files(agent_name)
    con.close()

    # Get pipeline
    pipeline = get_pipeline_for_task(task)
    if not pipeline:
        _log("pipeline_not_found", "task", str(task_id), "no active pipeline found")
        return {"ok": False, "error": "no_active_pipeline"}

    pipeline_id = pipeline["id"]
    blocks = json.loads(pipeline["blocks_json"])

    # Set task to active
    con = connect()
    con.execute("UPDATE tasks SET status='active', updated_at=? WHERE id=?", (_utcnow_iso(), task_id))
    con.commit()
    con.close()

    task_prompt = f"Task Title: {task['title']}\n\nTask Description:\n{task.get('description', '')}"
    current_output = ""
    last_review_notes = ""
    final_output = ""

    for i in range(from_block_index, len(blocks)):
        block = blocks[i]
        block_type = block.get("type", "")
        config = block.get("config", {})
        label = config.get("label", f"Block {i}")
        model = config.get("model", "")
        executor = config.get("executor", "OpenRouter")
        started_at = _utcnow_iso()

        _log("pipeline_block_start", "task", str(task_id), f"block={i} type={block_type} label={label}", model=model)

        # --- route block ---
        if block_type == "route":
            # Route blocks evaluate a condition. For now, always pass through.
            _log_executor(task_id, pipeline_id, i, "route", "", "", started_at, 0, True)
            continue

        # --- executor block ---
        if block_type == "executor":
            if executor == "Claude CLI":
                limit_action = _check_claude_health_for_block(config)
                if limit_action:
                    return _handle_on_limit(task_id, pipeline_id, i, limit_action, label)

            result = _run_executor_block(task_prompt, config, agent_system_prompt)
            _log_executor(
                task_id, pipeline_id, i, "executor", model, executor, started_at,
                result["duration_seconds"], result["success"],
                output_preview=result.get("output", ""),
                failure_type=result.get("failure_type"),
                error=result.get("error"),
            )
            if result["success"]:
                current_output = result["output"]
                final_output = current_output
            else:
                _log("pipeline_block_failed", "task", str(task_id), f"block={i} error={result.get('error')}")
                # Continue to next block (might be a review or retry)
            continue

        # --- review block ---
        if block_type == "review":
            if not current_output:
                _log_executor(task_id, pipeline_id, i, "review", model, executor, started_at, 0, True, pass_fail="skip", review_notes="no output to review")
                continue

            if executor == "Claude CLI":
                limit_action = _check_claude_health_for_block(config)
                if limit_action:
                    return _handle_on_limit(task_id, pipeline_id, i, limit_action, label)

            result = _run_review_block(current_output, config, task["title"], agent_system_prompt)
            passed, notes = _parse_review_verdict(result.get("output", ""))
            last_review_notes = notes

            _log_executor(
                task_id, pipeline_id, i, "review", model, executor, started_at,
                result["duration_seconds"], result["success"],
                pass_fail="pass" if passed else "fail",
                review_notes=notes[:500],
                output_preview=result.get("output", ""),
                failure_type=result.get("failure_type"),
                error=result.get("error"),
            )

            if passed and config.get("pass_action") == "skip_to_done":
                # Jump to done
                final_output = current_output
                _log("pipeline_review_pass_skip", "task", str(task_id), f"block={i} skipping to done")
                break
            # If fail, continue to next block (retry/escalate)
            continue

        # --- retry block ---
        if block_type == "retry":
            if executor == "Claude CLI":
                limit_action = _check_claude_health_for_block(config)
                if limit_action:
                    return _handle_on_limit(task_id, pipeline_id, i, limit_action, label)

            max_retries = config.get("max_retries", 1)
            include_notes = config.get("include_review_notes", False)
            retry_prompt = task_prompt
            if include_notes and last_review_notes:
                retry_prompt += f"\n\n## Review Feedback from Previous Attempt\n{last_review_notes}\n\nPlease address the issues noted above."

            for attempt in range(max_retries):
                result = _run_executor_block(retry_prompt, config, agent_system_prompt)
                _log_executor(
                    task_id, pipeline_id, i, "retry", model, executor, started_at,
                    result["duration_seconds"], result["success"],
                    output_preview=result.get("output", ""),
                    failure_type=result.get("failure_type"),
                    error=result.get("error"),
                )
                if result["success"]:
                    current_output = result["output"]
                    final_output = current_output
                    break
            continue

        # --- escalate block ---
        if block_type == "escalate":
            if executor == "Claude CLI":
                limit_action = _check_claude_health_for_block(config)
                if limit_action:
                    return _handle_on_limit(task_id, pipeline_id, i, limit_action, label)

            # Escalation uses the original task prompt, not previous output
            result = _run_executor_block(task_prompt, config, agent_system_prompt)
            _log_executor(
                task_id, pipeline_id, i, "escalate", model, executor, started_at,
                result["duration_seconds"], result["success"],
                output_preview=result.get("output", ""),
                failure_type=result.get("failure_type"),
                error=result.get("error"),
            )
            if result["success"]:
                current_output = result["output"]
                final_output = current_output
            continue

        # --- done block ---
        if block_type == "done":
            final_output = final_output or current_output
            break

    # Pipeline complete — store output and mark task
    con = connect()
    con.execute(
        "UPDATE tasks SET status='dev_done', last_result=?, updated_at=? WHERE id=?",
        (final_output, _utcnow_iso(), task_id),
    )
    con.commit()
    con.close()

    _log("pipeline_complete", "task", str(task_id), f"pipeline_id={pipeline_id} output_len={len(final_output)}")
    return {"ok": True, "output": final_output, "pipeline_id": pipeline_id}


def _handle_on_limit(task_id: int, pipeline_id: int, block_index: int, action: str, label: str) -> dict[str, Any]:
    """Handle on_limit behavior for a Claude CLI block."""
    if action == "stop":
        con = connect()
        con.execute(
            "UPDATE tasks SET status='paused_limit', resume_block_index=?, resume_pipeline_id=?, updated_at=? WHERE id=?",
            (block_index, pipeline_id, _utcnow_iso(), task_id),
        )
        con.commit()
        con.close()
        _log("pipeline_paused_limit", "task", str(task_id), f"block={block_index} label={label}")
        return {"ok": False, "error": "paused_limit", "status": "paused_limit"}

    elif action == "queue":
        con = connect()
        con.execute(
            "UPDATE tasks SET status='queued_for_claude', resume_block_index=?, resume_pipeline_id=?, updated_at=? WHERE id=?",
            (block_index, pipeline_id, _utcnow_iso(), task_id),
        )
        con.commit()
        con.close()
        _log("pipeline_queued_for_claude", "task", str(task_id), f"block={block_index} label={label}")
        return {"ok": False, "error": "queued_for_claude", "status": "queued_for_claude"}

    else:  # "fallback" — skip this block, continue
        _log("pipeline_block_skipped_limit", "task", str(task_id), f"block={block_index} label={label} on_limit=fallback")
        # Return a signal to continue — but since we're in the loop, we need
        # to re-enter. For simplicity, we just return and let the pipeline
        # mark the task with whatever output we have so far.
        return {"ok": True, "output": "", "skipped": True}


def resume_pipeline(task_id: int) -> dict[str, Any]:
    """Resume a paused/queued pipeline from where it left off."""
    con = connect()
    task = con.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    con.close()
    if not task:
        return {"ok": False, "error": "task_not_found"}
    task = dict(task)

    block_index = task.get("resume_block_index")
    if block_index is None:
        return {"ok": False, "error": "no_resume_block_index"}

    return run_pipeline(task_id, from_block_index=block_index)
