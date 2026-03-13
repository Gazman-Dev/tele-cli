from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AppPaths:
    root: Path
    app_lock: Path
    setup_lock: Path
    runtime: Path
    auth: Path
    config: Path
    recovery_log: Path
    terminal_log: Path


def default_state_dir() -> Path:
    custom = os.environ.get("MINIC_STATE_DIR")
    if custom:
        return Path(custom).expanduser().resolve()
    return Path.home().joinpath(".tele-cli")


def build_paths(state_dir: Path | None = None) -> AppPaths:
    root = (state_dir or default_state_dir()).expanduser().resolve()
    return AppPaths(
        root=root,
        app_lock=root / "app.lock",
        setup_lock=root / "setup.lock",
        runtime=root / "runtime.json",
        auth=root / "auth.json",
        config=root / "config.json",
        recovery_log=root / "recovery.log",
        terminal_log=root / "terminal.log",
    )
