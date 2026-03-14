from __future__ import annotations

from typing import Optional

from app_meta import APP_VERSION
from core.json_store import load_json, save_json
from core.locks import LockFile
from core.logging_utils import append_recovery_log
from core.models import AuthState, Config, RuntimeState
from core.paths import AppPaths
from core.process import describe_process, make_lock_metadata, process_exists, read_process_command, safe_kill
from core.prompts import ask_choice
from integrations.telegram import TelegramClient
from .codex_runtime import CodexSession
from .runtime import ServiceRuntime


def isatty() -> bool:
    try:
        import sys

        return sys.stdin.isatty()
    except Exception:
        return False


def start_codex_session(
    config: Config,
    auth: AuthState,
    runtime: ServiceRuntime,
    runtime_state: RuntimeState,
    metadata,
    app_lock: LockFile,
    telegram: TelegramClient,
    handle_output,
) -> CodexSession:
    runtime.start_codex()
    codex = CodexSession(config.codex_command, handle_output)
    runtime_state.codex_pid = codex.start()
    metadata.child_codex_pid = runtime_state.codex_pid
    app_lock.write(metadata)
    save_json(app_lock.path.parent / "runtime.json", runtime_state.to_dict())
    telegram.send_message(auth.telegram_chat_id, "Tele Cli service connected. Starting Codex session.")
    return codex


def reset_auth(paths: AppPaths) -> None:
    auth = load_json(paths.auth, AuthState.from_dict)
    if not auth:
        return
    auth.telegram_user_id = None
    auth.telegram_chat_id = None
    auth.paired_at = None
    auth.pairing_code = None
    auth.pending_user_id = None
    auth.pending_chat_id = None
    auth.pending_issued_at = None
    save_json(paths.auth, auth.to_dict())


def prepare_service_lock(paths: AppPaths) -> tuple[LockFile, object]:
    app_lock = LockFile(paths.app_lock)
    handle_service_conflict(paths, app_lock)
    metadata = make_lock_metadata(mode="service", app_version=APP_VERSION, cwd=paths.root)
    app_lock.write(metadata)
    return app_lock, metadata


def handle_service_conflict(paths: AppPaths, app_lock: LockFile) -> None:
    inspection = app_lock.inspect()
    if not inspection.exists or not inspection.metadata:
        return

    metadata = inspection.metadata
    if inspection.live and inspection.same_app:
        print("Another app instance appears to be running.")
        print(describe_process(metadata))
        choice = ask_choice("Resolve live app conflict", ["kill", "ignore", "exit"], default="exit")
        append_recovery_log(paths.recovery_log, f"service conflict -> {choice} pid={metadata.pid}")
        if choice == "kill":
            safe_kill(metadata.pid)
            if metadata.child_codex_pid and is_owned_codex(metadata.child_codex_pid, metadata.cwd):
                safe_kill(metadata.child_codex_pid)
            app_lock.clear()
        elif choice == "exit":
            raise SystemExit(1)
        return

    print("A stale app lock was found.")
    print(describe_process(metadata))
    choice = ask_choice("Resolve stale app lock", ["heal", "ignore", "exit"], default="heal")
    append_recovery_log(paths.recovery_log, f"stale service lock -> {choice} pid={metadata.pid}")
    if choice == "heal":
        if metadata.child_codex_pid and process_exists(metadata.child_codex_pid):
            print("A Codex process from a previous run may still be active.")
            orphan_choice = ask_choice("Resolve orphaned Codex", ["kill", "ignore", "exit"], default="ignore")
            append_recovery_log(
                paths.recovery_log,
                f"orphan codex -> {orphan_choice} pid={metadata.child_codex_pid}",
            )
            if orphan_choice == "kill" and is_owned_codex(metadata.child_codex_pid, metadata.cwd):
                safe_kill(metadata.child_codex_pid)
            elif orphan_choice == "exit":
                raise SystemExit(1)
        app_lock.clear()
    elif choice == "exit":
        raise SystemExit(1)


def is_owned_codex(pid: int, cwd: Optional[str]) -> bool:
    command = read_process_command(pid)
    if not command or "codex" not in command:
        return False
    if cwd and cwd not in command:
        return False
    return True
