"""Claude CLI Executor — runs tasks via `claude -p` subprocess.

Classifies every invocation result into a failure type and returns
a standardised result dict compatible with the pipeline executor.
"""
from __future__ import annotations

import os
import re
import subprocess
import time
from typing import Any

# ---------------------------------------------------------------------------
# Failure-type constants
# ---------------------------------------------------------------------------
CLAUDE_SUCCESS = "CLAUDE_SUCCESS"
CLAUDE_FAIL_AUTH = "CLAUDE_FAIL_AUTH"
CLAUDE_FAIL_RATE_LIMIT = "CLAUDE_FAIL_RATE_LIMIT"
CLAUDE_FAIL_DAILY_LIMIT = "CLAUDE_FAIL_DAILY_LIMIT"
CLAUDE_FAIL_TIMEOUT = "CLAUDE_FAIL_TIMEOUT"
CLAUDE_FAIL_ERROR = "CLAUDE_FAIL_ERROR"

# ---------------------------------------------------------------------------
# Detection patterns (case-insensitive)
# ---------------------------------------------------------------------------
_AUTH_PATTERNS = re.compile(
    r"unauthorized|login|session.?expired|auth|token", re.IGNORECASE
)
_RATE_LIMIT_PATTERNS = re.compile(
    r"rate.?limit|too many requests|throttled|capacity|try again later",
    re.IGNORECASE,
)
_DAILY_LIMIT_PATTERNS = re.compile(
    r"daily.?limit|usage.?limit|limit.?reached|quota.?exceeded",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Rolling-average tracker for detecting stealth rate limits
# ---------------------------------------------------------------------------
_recent_durations: list[float] = []
_MAX_HISTORY = 20


def _rolling_avg() -> float:
    if not _recent_durations:
        return 0.0
    return sum(_recent_durations) / len(_recent_durations)


def _record_duration(d: float) -> None:
    _recent_durations.append(d)
    if len(_recent_durations) > _MAX_HISTORY:
        _recent_durations.pop(0)


# ---------------------------------------------------------------------------
# Consecutive rate-limit tracking (for daily-limit escalation)
# ---------------------------------------------------------------------------
_consecutive_rate_limits: list[float] = []  # timestamps of consecutive RL hits


def _check_daily_from_consecutive(ts: float) -> bool:
    """Return True if N consecutive rate limits occurred within window."""
    max_consecutive = int(os.getenv("CLAUDE_CONSECUTIVE_RATE_LIMITS_FOR_DAILY", "3"))
    window_minutes = int(os.getenv("CLAUDE_RATE_LIMIT_WINDOW_MINUTES", "10"))
    window_seconds = window_minutes * 60

    _consecutive_rate_limits.append(ts)
    # Trim old entries outside window
    cutoff = ts - window_seconds
    while _consecutive_rate_limits and _consecutive_rate_limits[0] < cutoff:
        _consecutive_rate_limits.pop(0)

    return len(_consecutive_rate_limits) >= max_consecutive


def _reset_consecutive() -> None:
    _consecutive_rate_limits.clear()


# ---------------------------------------------------------------------------
# Main executor
# ---------------------------------------------------------------------------

def execute_claude_cli(prompt: str) -> dict[str, Any]:
    """Run ``claude -p "{prompt}"`` and return a standardised result dict.

    Result schema::

        {
            "success": bool,
            "output": str,
            "executor": "claude_cli",
            "duration_seconds": float,
            "error": str | None,
            "failure_type": str,
        }
    """
    timeout = int(os.getenv("CLAUDE_CLI_TIMEOUT_SECONDS", "300"))

    start = time.monotonic()
    try:
        proc = subprocess.run(
            ["claude", "-p", prompt],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        elapsed = time.monotonic() - start
        _record_duration(elapsed)

        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        combined = f"{stdout}\n{stderr}"

        # --- classify ---
        if proc.returncode != 0:
            if _AUTH_PATTERNS.search(combined):
                return _result(False, stdout, elapsed, stderr, CLAUDE_FAIL_AUTH)
            if _DAILY_LIMIT_PATTERNS.search(combined):
                return _result(False, stdout, elapsed, stderr, CLAUDE_FAIL_DAILY_LIMIT)
            if _RATE_LIMIT_PATTERNS.search(combined):
                ts = time.time()
                if _check_daily_from_consecutive(ts):
                    return _result(False, stdout, elapsed, stderr, CLAUDE_FAIL_DAILY_LIMIT)
                return _result(False, stdout, elapsed, stderr, CLAUDE_FAIL_RATE_LIMIT)
            return _result(False, stdout, elapsed, stderr, CLAUDE_FAIL_ERROR)

        # exit-code 0 — but check for empty/truncated output with suspiciously long response
        if not stdout.strip():
            # Empty output with exit code 0 might be stealth rate limiting
            avg = _rolling_avg()
            if avg > 0 and elapsed > avg * 3:
                ts = time.time()
                if _check_daily_from_consecutive(ts):
                    return _result(False, "", elapsed, "empty output, suspected daily limit", CLAUDE_FAIL_DAILY_LIMIT)
                return _result(False, "", elapsed, "empty output, suspected rate limit", CLAUDE_FAIL_RATE_LIMIT)
            return _result(False, "", elapsed, "empty output", CLAUDE_FAIL_ERROR)

        # Check stdout/stderr for rate limit signals even on success exit code
        if _DAILY_LIMIT_PATTERNS.search(combined):
            return _result(False, stdout, elapsed, "daily limit signal in output", CLAUDE_FAIL_DAILY_LIMIT)
        if _RATE_LIMIT_PATTERNS.search(combined):
            ts = time.time()
            if _check_daily_from_consecutive(ts):
                return _result(False, stdout, elapsed, "consecutive rate limits → daily", CLAUDE_FAIL_DAILY_LIMIT)
            return _result(False, stdout, elapsed, "rate limit signal in output", CLAUDE_FAIL_RATE_LIMIT)

        # Genuine success
        _reset_consecutive()
        return _result(True, stdout, elapsed, None, CLAUDE_SUCCESS)

    except subprocess.TimeoutExpired:
        elapsed = time.monotonic() - start
        return _result(False, "", elapsed, f"timeout after {timeout}s", CLAUDE_FAIL_TIMEOUT)

    except FileNotFoundError:
        elapsed = time.monotonic() - start
        return _result(False, "", elapsed, "claude CLI not found on PATH", CLAUDE_FAIL_ERROR)

    except Exception as exc:  # noqa: BLE001
        elapsed = time.monotonic() - start
        return _result(False, "", elapsed, str(exc), CLAUDE_FAIL_ERROR)


def _result(
    success: bool,
    output: str,
    duration: float,
    error: str | None,
    failure_type: str,
) -> dict[str, Any]:
    return {
        "success": success,
        "output": output,
        "executor": "claude_cli",
        "duration_seconds": round(duration, 3),
        "error": error,
        "failure_type": failure_type,
    }
