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
    database: Path
    artifacts: Path
    memory: Path
    lessons: Path
    session_memory: Path
    workspace: Path
    workspace_topics: Path
    auth: Path
    config: Path
    sleep_state: Path
    terminal_log: Path
    performance_log: Path
    logging_health: Path
    logging_emergency_log: Path


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
        database=root / "tele_cli.db",
        artifacts=root / "artifacts",
        memory=root / "memory",
        lessons=root / "memory" / "lessons",
        session_memory=root / "memory" / "sessions",
        workspace=root / "workspace",
        workspace_topics=root / "workspace" / "topics",
        auth=root / "auth.json",
        config=root / "config.json",
        sleep_state=root / "sleep_state.json",
        terminal_log=root / "terminal.log",
        performance_log=root / "performance.log",
        logging_health=root / "logging_health.json",
        logging_emergency_log=root / "logging_emergency.log",
    )
