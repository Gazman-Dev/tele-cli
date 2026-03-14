from __future__ import annotations

from .. import APP_VERSION
from ..core.locks import LockFile
from ..core.logging_utils import append_recovery_log
from ..core.models import SetupState, utc_now
from ..core.paths import AppPaths
from ..core.process import describe_process, make_lock_metadata, process_exists, safe_kill
from ..core.prompts import ask_choice
from .state import load_setup_state, save_setup_state


def initialize_setup(paths: AppPaths) -> tuple[LockFile, SetupState]:
    app_lock = LockFile(paths.app_lock)
    handle_existing_app_lock(app_lock, paths)
    app_lock.write(make_lock_metadata(mode="setup", app_version=APP_VERSION, cwd=paths.root))
    setup_state = handle_existing_setup(paths)
    return app_lock, setup_state


def handle_existing_app_lock(app_lock: LockFile, paths: AppPaths) -> None:
    inspection = app_lock.inspect()
    if not inspection.exists or not inspection.metadata:
        return
    metadata = inspection.metadata
    if inspection.live and inspection.same_app:
        print("Another app instance appears to be running.")
        print(describe_process(metadata))
        choice = ask_choice("Resolve live app conflict", ["kill", "ignore", "exit"], default="exit")
        append_recovery_log(paths.recovery_log, f"live app conflict -> {choice} pid={metadata.pid}")
        if choice == "kill":
            safe_kill(metadata.pid)
            app_lock.clear()
        elif choice == "exit":
            raise SystemExit(1)
    else:
        print("A stale app lock was found.")
        print(describe_process(metadata))
        choice = ask_choice("Resolve stale app lock", ["heal", "ignore", "exit"], default="heal")
        append_recovery_log(paths.recovery_log, f"stale app lock -> {choice} pid={metadata.pid}")
        if choice == "heal":
            if metadata.child_codex_pid and process_exists(metadata.child_codex_pid):
                print("A Codex process from a previous run may still be active.")
            app_lock.clear()
        elif choice == "exit":
            raise SystemExit(1)


def handle_existing_setup(paths: AppPaths) -> SetupState:
    existing = load_setup_state(paths)
    if not existing or existing.status == "completed":
        state = SetupState(status="started", pid=0, timestamp=utc_now())
        save_setup_state(paths, state)
        return state

    if existing.status == "started" and existing.pid and process_exists(existing.pid):
        print("A setup run is already marked as active.")
        choice = ask_choice("Resolve setup conflict", ["kill", "ignore", "exit"], default="exit")
        append_recovery_log(paths.recovery_log, f"active setup conflict -> {choice} pid={existing.pid}")
        if choice == "kill":
            safe_kill(existing.pid)
        elif choice == "exit":
            raise SystemExit(1)
        return SetupState(status="started", pid=0, timestamp=utc_now())

    print("A previous setup did not finish.")
    print(
        f"npm_installed={existing.npm_installed} codex_installed={existing.codex_installed} "
        f"telegram_token_saved={existing.telegram_token_saved} telegram_validated={existing.telegram_validated}"
    )
    choice = ask_choice("Resolve interrupted setup", ["resume", "restart", "ignore", "exit"], default="resume")
    append_recovery_log(paths.recovery_log, f"stale setup -> {choice}")
    if choice == "restart":
        state = SetupState(status="started", pid=0, timestamp=utc_now())
        save_setup_state(paths, state)
        return state
    if choice == "exit":
        raise SystemExit(1)
    existing.status = "started"
    existing.timestamp = utc_now()
    save_setup_state(paths, existing)
    return existing
