#!/usr/bin/env python3
"""ZeroClaw + OpenClaw auto-startup script.

Usage:
    python start.py                          # Auto-discover zip + start services
    python start.py --zip path/to/code.zip   # Explicit zip + start services
    python start.py --check                  # Just check if services are running
    python start.py --stop                   # Stop background services

On startup (without --check/--stop), the script will:
  1. Look for a zip in /a0/usr/uploads/ (Agent Zero upload dir) or use --zip
  2. Extract it into the project directory if found
  3. Start ZeroClaw on port 9000 (which auto-launches OpenClaw on port 9100)
"""

import os
import sys
import socket
import subprocess
import time
import signal
import zipfile
import shutil
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
DATA_DIR = PROJECT_DIR / "data"
PID_FILE = DATA_DIR / "zeroclaw.pid"
LOG_FILE = DATA_DIR / "zeroclaw.log"

# Agent Zero filesystem paths
A0_UPLOADS_DIR = Path(os.getenv("A0_UPLOADS_DIR", "/a0/usr/uploads"))
A0_WORKDIR = Path(os.getenv("A0_WORKDIR", "/a0/usr/workdir/zeroclaw"))

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


def find_upload_zip() -> str | None:
    """Auto-detect the newest zip file in the Agent Zero uploads directory.

    Scans /a0/usr/uploads/ (or A0_UPLOADS_DIR env override) for .zip files
    and returns the most recently modified one. Returns None if the directory
    doesn't exist or contains no zips.
    """
    if not A0_UPLOADS_DIR.is_dir():
        return None

    zips = sorted(A0_UPLOADS_DIR.glob("*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not zips:
        return None

    print(f"[discover] Found {len(zips)} zip(s) in {A0_UPLOADS_DIR}")
    chosen = zips[0]
    print(f"[discover] Using newest: {chosen.name}")
    return str(chosen)


def deploy_zip(zip_path: str):
    """Extract a zip file into the project directory, updating code in place.

    Safely extracts app/, data/agents/, templates, and other project files
    from a zip archive. Existing files are overwritten; new files are added.
    The zip can contain files at the root or inside a single top-level folder.
    """
    zp = Path(zip_path).resolve()
    if not zp.exists():
        print(f"[deploy] ERROR: Zip not found: {zp}")
        return False
    if not zipfile.is_zipfile(str(zp)):
        print(f"[deploy] ERROR: Not a valid zip file: {zp}")
        return False

    print(f"[deploy] Extracting {zp.name} into {PROJECT_DIR}...")

    with zipfile.ZipFile(str(zp), "r") as z:
        names = z.namelist()

        # Detect if everything is nested under a single top-level directory
        # e.g. "dashboard_scaffold/app/main.py" → strip "dashboard_scaffold/"
        prefix = ""
        top_dirs = set()
        for n in names:
            parts = n.split("/")
            if len(parts) > 1:
                top_dirs.add(parts[0])
        if len(top_dirs) == 1 and all(n.startswith(list(top_dirs)[0] + "/") for n in names if n):
            prefix = list(top_dirs)[0] + "/"
            print(f"[deploy] Detected top-level folder '{prefix.rstrip('/')}' — stripping it")

        extracted = 0
        for info in z.infolist():
            # Skip directories
            if info.is_dir():
                continue

            # Strip prefix if present
            rel = info.filename
            if prefix and rel.startswith(prefix):
                rel = rel[len(prefix):]
            if not rel:
                continue

            dest = PROJECT_DIR / rel
            dest.parent.mkdir(parents=True, exist_ok=True)

            with z.open(info) as src, open(dest, "wb") as dst:
                shutil.copyfileobj(src, dst)
            extracted += 1

        print(f"[deploy] Extracted {extracted} files")

    return True


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

    # Deploy zip if provided or auto-discovered
    zip_path = None
    if "--zip" in sys.argv:
        idx = sys.argv.index("--zip")
        if idx + 1 >= len(sys.argv):
            print("[deploy] ERROR: --zip requires a path argument")
            sys.exit(1)
        zip_path = sys.argv[idx + 1]
    else:
        # Auto-discover from Agent Zero uploads dir (/a0/usr/uploads/)
        zip_path = find_upload_zip()

    if zip_path:
        if not deploy_zip(zip_path):
            sys.exit(1)
        # If services are running, restart them to pick up new code
        status = check_status()
        if status["zeroclaw"]:
            print("[deploy] Restarting services to pick up new code...")
            stop_services()
            time.sleep(1)

    ok = start_services()
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
