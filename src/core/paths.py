from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class AppPaths:
    root: Path
    app_lock: Path
    setup_lock: Path
    runtime: Path
    auth: Path
    config: Path
    sessions: Path
    telegram_updates: Path
    approvals: Path
    codex_server: Path
    sleep_state: Path
    recovery_log: Path
    terminal_log: Path
    performance_log: Path


def default_state_dir() -> Path:
    custom = os.environ.get("MINIC_STATE_DIR")
    if custom:
        return Path(custom).expanduser().resolve()
    return Path.home().joinpath(".tele-cli")


def build_paths(state_dir: Optional[Path | str] = None) -> AppPaths:
    root_input = state_dir or default_state_dir()
    root = Path(root_input).expanduser().resolve()
    return AppPaths(
        root=root,
        app_lock=root / "app.lock",
        setup_lock=root / "setup.lock",
        runtime=root / "runtime.json",
        auth=root / "auth.json",
        config=root / "config.json",
        sessions=root / "sessions.json",
        telegram_updates=root / "telegram_updates.json",
        approvals=root / "approvals.json",
        codex_server=root / "codex_server.json",
        sleep_state=root / "sleep_state.json",
        recovery_log=root / "recovery.log",
        terminal_log=root / "terminal.log",
        performance_log=root / "performance.log",
    )
