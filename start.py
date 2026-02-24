#!/usr/bin/env python3
"""ZeroClaw + OpenClaw auto-startup script.

Usage:
    python start.py          # Start ZeroClaw (OpenClaw auto-starts with it)
    python start.py --check  # Just check if services are running
    python start.py --stop   # Stop background services

ZeroClaw (port 9000) handles the dashboard, task board, and pipelines.
OpenClaw (port 9100) is auto-launched by ZeroClaw on startup.
"""

import os
import sys
import socket
import subprocess
import time
import signal
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
DATA_DIR = PROJECT_DIR / "data"
PID_FILE = DATA_DIR / "zeroclaw.pid"
LOG_FILE = DATA_DIR / "zeroclaw.log"

ZEROCLAW_PORT = int(os.getenv("ZEROCLAW_PORT", "9000"))
OPENCLAW_PORT = int(os.getenv("OPENCLAW_PORT", "9100"))


def tcp_listening(port: int, host: str = "127.0.0.1", timeout: float = 0.3) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def read_pid() -> int | None:
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text().strip())
            # Check if process is still alive
            if sys.platform == "win32":
                import ctypes
                kernel32 = ctypes.windll.kernel32
                handle = kernel32.OpenProcess(0x0400, False, pid)  # PROCESS_QUERY_INFORMATION
                if handle:
                    kernel32.CloseHandle(handle)
                    return pid
            else:
                os.kill(pid, 0)
                return pid
        except (ValueError, OSError, ProcessLookupError):
            PID_FILE.unlink(missing_ok=True)
    return None


def check_status() -> dict:
    """Check service status. Returns dict with 'zeroclaw' and 'openclaw' bools."""
    zc = tcp_listening(ZEROCLAW_PORT)
    oc = tcp_listening(OPENCLAW_PORT)
    pid = read_pid()
    return {"zeroclaw": zc, "openclaw": oc, "pid": pid}


def stop_services():
    """Stop ZeroClaw (OpenClaw goes down with it if child process)."""
    pid = read_pid()
    if pid:
        print(f"[stop] Sending SIGTERM to PID {pid}...")
        try:
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                               capture_output=True, timeout=5)
            else:
                os.kill(pid, signal.SIGTERM)
        except Exception as e:
            print(f"[stop] Error: {e}")
        PID_FILE.unlink(missing_ok=True)
    else:
        print("[stop] No tracked PID found.")

    # Wait for ports to free up
    for _ in range(10):
        if not tcp_listening(ZEROCLAW_PORT):
            break
        time.sleep(0.5)

    status = check_status()
    if not status["zeroclaw"]:
        print("[stop] ZeroClaw stopped.")
    else:
        print("[stop] WARNING: Port still in use. May need manual cleanup.")


def start_services():
    """Start ZeroClaw in the background. OpenClaw auto-starts via main.py startup."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # Already running?
    if tcp_listening(ZEROCLAW_PORT):
        pid = read_pid()
        print(f"[start] ZeroClaw already running on :{ZEROCLAW_PORT} (PID {pid or '?'})")
        if tcp_listening(OPENCLAW_PORT):
            print(f"[start] OpenClaw already running on :{OPENCLAW_PORT}")
        else:
            print(f"[start] OpenClaw not detected on :{OPENCLAW_PORT} — it should auto-start shortly")
        return True

    print(f"[start] Launching ZeroClaw on :{ZEROCLAW_PORT}...")

    cmd = [
        sys.executable, "-m", "uvicorn",
        "app.main:app",
        "--host", "0.0.0.0",
        "--port", str(ZEROCLAW_PORT),
        "--log-level", "info",
    ]

    log_fh = open(LOG_FILE, "ab")

    if sys.platform == "win32":
        # Windows: use CREATE_NEW_PROCESS_GROUP so it survives parent exit
        proc = subprocess.Popen(
            cmd,
            cwd=str(PROJECT_DIR),
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS,
        )
    else:
        proc = subprocess.Popen(
            cmd,
            cwd=str(PROJECT_DIR),
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    PID_FILE.write_text(str(proc.pid))
    print(f"[start] PID {proc.pid} — waiting for service...")

    # Wait for ZeroClaw to come up
    for i in range(20):
        time.sleep(0.5)
        if tcp_listening(ZEROCLAW_PORT):
            print(f"[start] ZeroClaw is UP on :{ZEROCLAW_PORT}")
            # Give OpenClaw a moment to auto-start
            for j in range(10):
                time.sleep(0.5)
                if tcp_listening(OPENCLAW_PORT):
                    print(f"[start] OpenClaw is UP on :{OPENCLAW_PORT}")
                    break
            else:
                print(f"[start] OpenClaw not yet on :{OPENCLAW_PORT} (may still be starting)")
            return True

    print(f"[start] TIMEOUT waiting for ZeroClaw. Check {LOG_FILE}")
    return False


def main():
    if "--check" in sys.argv:
        status = check_status()
        zc = "UP" if status["zeroclaw"] else "DOWN"
        oc = "UP" if status["openclaw"] else "DOWN"
        pid = status["pid"] or "none"
        print(f"ZeroClaw :{ZEROCLAW_PORT} = {zc}  |  OpenClaw :{OPENCLAW_PORT} = {oc}  |  PID = {pid}")
        sys.exit(0 if status["zeroclaw"] else 1)

    if "--stop" in sys.argv:
        stop_services()
        sys.exit(0)

    ok = start_services()
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
