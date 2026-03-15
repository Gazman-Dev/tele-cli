from __future__ import annotations

from dataclasses import dataclass

from app_meta import APP_VERSION
from core.locks import LockFile
from core.logging_utils import append_recovery_log
from core.models import LockMetadata, SetupState, utc_now
from core.paths import AppPaths
from core.process import describe_process, make_lock_metadata, process_exists, safe_kill
from core.prompts import ask_choice
from .state import load_setup_state, save_setup_state


@dataclass
class AppLockConflict:
    kind: str
    metadata: LockMetadata


@dataclass
class ExistingSetupConflict:
    kind: str
    state: SetupState


@dataclass
class SetupRecoveryChoices:
    app_lock_choice: str | None = None
    setup_choice: str | None = None


def initialize_setup(paths: AppPaths, choices: SetupRecoveryChoices | None = None) -> tuple[LockFile, SetupState]:
    app_lock = LockFile(paths.app_lock)
    handle_existing_app_lock(app_lock, paths, choice=choices.app_lock_choice if choices else None)
    app_lock.write(make_lock_metadata(mode="setup", app_version=APP_VERSION, cwd=paths.root))
    setup_state = handle_existing_setup(paths, choice=choices.setup_choice if choices else None)
    return app_lock, setup_state


def inspect_existing_app_lock(app_lock: LockFile) -> AppLockConflict | None:
    inspection = app_lock.inspect()
    if not inspection.exists or not inspection.metadata:
        return None
    metadata = inspection.metadata
    if inspection.live and inspection.same_app:
        return AppLockConflict(kind="live", metadata=metadata)
    return AppLockConflict(kind="stale", metadata=metadata)


def handle_existing_app_lock(app_lock: LockFile, paths: AppPaths, choice: str | None = None) -> None:
    conflict = inspect_existing_app_lock(app_lock)
    if not conflict:
        return
    metadata = conflict.metadata
    if conflict.kind == "live":
        print("Another app instance appears to be running.")
        print(describe_process(metadata))
        selected = choice or ask_choice("Resolve live app conflict", ["kill", "ignore", "exit"], default="exit")
        append_recovery_log(paths.recovery_log, f"live app conflict -> {selected} pid={metadata.pid}")
        if selected == "kill":
            safe_kill(metadata.pid)
            app_lock.clear()
        elif selected == "exit":
            raise SystemExit(1)
        return

    print("A stale app lock was found.")
    print(describe_process(metadata))
    selected = choice or ask_choice("Resolve stale app lock", ["heal", "ignore", "exit"], default="heal")
    append_recovery_log(paths.recovery_log, f"stale app lock -> {selected} pid={metadata.pid}")
    if selected == "heal":
        if metadata.child_codex_pid and process_exists(metadata.child_codex_pid):
            print("A Codex process from a previous run may still be active.")
        app_lock.clear()
    elif selected == "exit":
        raise SystemExit(1)


def inspect_existing_setup(paths: AppPaths) -> ExistingSetupConflict | None:
    existing = load_setup_state(paths)
    if not existing or existing.status == "completed":
        return None

    if existing.status == "started" and existing.pid and process_exists(existing.pid):
        return ExistingSetupConflict(kind="active", state=existing)
    return ExistingSetupConflict(kind="interrupted", state=existing)


def handle_existing_setup(paths: AppPaths, choice: str | None = None) -> SetupState:
    conflict = inspect_existing_setup(paths)
    if not conflict:
        state = SetupState(status="started", pid=0, timestamp=utc_now())
        save_setup_state(paths, state)
        return state

    existing = conflict.state
    if conflict.kind == "active":
        print("A setup run is already marked as active.")
        selected = choice or ask_choice("Resolve setup conflict", ["kill", "ignore", "exit"], default="exit")
        append_recovery_log(paths.recovery_log, f"active setup conflict -> {selected} pid={existing.pid}")
        if selected == "kill":
            safe_kill(existing.pid)
        elif selected == "exit":
            raise SystemExit(1)
        return SetupState(status="started", pid=0, timestamp=utc_now())

    print("A previous setup did not finish.")
    print(
        f"npm_installed={existing.npm_installed} codex_installed={existing.codex_installed} "
        f"telegram_token_saved={existing.telegram_token_saved} telegram_validated={existing.telegram_validated}"
    )
    selected = choice or ask_choice("Resolve interrupted setup", ["resume", "restart", "ignore", "exit"], default="resume")
    append_recovery_log(paths.recovery_log, f"stale setup -> {selected}")
    if selected == "restart":
        state = SetupState(status="started", pid=0, timestamp=utc_now())
        save_setup_state(paths, state)
        return state
    if selected == "exit":
        raise SystemExit(1)
    existing.status = "started"
    existing.timestamp = utc_now()
    save_setup_state(paths, existing)
    return existing
