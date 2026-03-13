from __future__ import annotations

import getpass
import os
import signal
import socket
import subprocess
import sys
from pathlib import Path
from typing import Optional

from .models import LockMetadata, utc_now


def current_command() -> list[str]:
    return [sys.executable, *sys.argv]


def make_lock_metadata(mode: str, app_version: str, cwd: Path) -> LockMetadata:
    return LockMetadata(
        pid=os.getpid(),
        hostname=socket.gethostname(),
        username=getpass.getuser(),
        started_at=process_started_at(os.getpid()),
        mode=mode,
        timestamp=utc_now(),
        app_version=app_version,
        command=current_command(),
        cwd=str(cwd),
    )


def process_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def read_process_command(pid: int) -> Optional[str]:
    if sys.platform.startswith("linux"):
        path = Path("/proc") / str(pid) / "cmdline"
        if path.exists():
            data = path.read_bytes().replace(b"\x00", b" ").decode("utf-8", errors="ignore").strip()
            return data or None
    try:
        result = subprocess.run(
            ["ps", "-o", "command=", "-p", str(pid)],
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return None
    command = result.stdout.strip()
    return command or None


def process_started_at(pid: int) -> Optional[str]:
    try:
        result = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return None
    value = result.stdout.strip()
    return value or None


def is_same_app_process(metadata: LockMetadata) -> bool:
    command = read_process_command(metadata.pid)
    if not command:
        return False
    own_markers = [Path(arg).name for arg in metadata.command if arg]
    return any(marker and marker in command for marker in own_markers)


def safe_kill(pid: int) -> None:
    os.kill(pid, signal.SIGTERM)


def describe_process(metadata: LockMetadata) -> str:
    return (
        f"PID={metadata.pid} mode={metadata.mode} host={metadata.hostname} "
        f"at={metadata.timestamp} child_codex_pid={metadata.child_codex_pid}"
    )
