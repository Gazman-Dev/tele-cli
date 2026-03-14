from __future__ import annotations

import time
import uuid
from typing import Optional

from . import APP_VERSION
from .codex_runtime import CodexSession
from .debug_mirror import DebugMirror
from .json_store import load_json, save_json
from .locks import LockFile
from .logging_utils import append_recovery_log
from .models import AuthState, Config, RuntimeState, utc_now
from .paths import AppPaths
from .process import describe_process, make_lock_metadata, process_exists, read_process_command, safe_kill
from .prompts import ask_choice
from .setup_flow import complete_pending_pairing
from .recorder import Recorder
from .runtime import ServiceRuntime
from .telegram import TelegramClient, has_pending_pairing, is_auth_paired, register_pairing_request


def run_service(paths: AppPaths) -> None:
    paths.root.mkdir(parents=True, exist_ok=True)
    config = load_json(paths.config, Config.from_dict)
    auth = load_json(paths.auth, AuthState.from_dict)
    if not config or not auth:
        raise RuntimeError("Run setup first.")

    app_lock = LockFile(paths.app_lock)
    _handle_service_conflict(paths, app_lock)
    metadata = make_lock_metadata(mode="service", app_version=APP_VERSION, cwd=paths.root)
    app_lock.write(metadata)

    runtime_state = RuntimeState(
        session_id=str(uuid.uuid4()),
        service_state="RUNNING",
        codex_state="STOPPED",
        telegram_state="STOPPED",
        recorder_state="STOPPED",
        debug_state="STOPPED",
    )
    runtime = ServiceRuntime(runtime_state)
    recorder = Recorder(paths.terminal_log)
    debug = DebugMirror()
    telegram = TelegramClient(auth.bot_token)

    runtime.start_recorder()
    recorder.start()
    runtime.start_debug()
    debug.start()
    runtime.start_telegram()

    def handle_output(source: str, line: str) -> None:
        recorder.record(source, line)
        debug.emit(source, line)
        runtime_state.last_output_at = utc_now()
        save_json(paths.runtime, runtime_state.to_dict())
        if auth.telegram_chat_id:
            telegram.send_message(auth.telegram_chat_id, f"[{source}] {line[:3500]}")

    codex = None
    if is_auth_paired(auth):
        codex = _start_codex_session(config, auth, runtime, runtime_state, metadata, app_lock, telegram, handle_output)
    save_json(paths.runtime, runtime_state.to_dict())
    append_recovery_log(paths.recovery_log, f"service started session_id={runtime_state.session_id}")

    offset = None
    try:
        if has_pending_pairing(auth) and isatty():
            complete_pending_pairing(paths, auth, telegram, allow_empty=True)
            if is_auth_paired(auth) and codex is None:
                codex = _start_codex_session(
                    config,
                    auth,
                    runtime,
                    runtime_state,
                    metadata,
                    app_lock,
                    telegram,
                    handle_output,
                )
        while True:
            for update in telegram.get_updates(offset=offset, timeout=20):
                offset = update["update_id"] + 1
                ok, status = register_pairing_request(auth, update)
                save_json(paths.auth, auth.to_dict())
                if status == "already-paired":
                    chat_id = update.get("message", {}).get("chat", {}).get("id")
                    if chat_id:
                        telegram.send_message(chat_id, "This bot is already paired to another chat.")
                    continue
                if status == "code-issued":
                    if auth.pending_chat_id and auth.pairing_code:
                        telegram.send_message(
                            auth.pending_chat_id,
                            f"Pairing code: {auth.pairing_code}. Enter this code in the local Tele Cli terminal to authorize this chat.",
                        )
                    print(
                        "Pairing requested. "
                        f"chat_id={auth.pending_chat_id} user_id={auth.pending_user_id} code={auth.pairing_code}"
                    )
                    if isatty():
                        if complete_pending_pairing(paths, auth, telegram, allow_empty=True) and codex is None:
                            codex = _start_codex_session(
                                config,
                                auth,
                                runtime,
                                runtime_state,
                                metadata,
                                app_lock,
                                telegram,
                                handle_output,
                            )
                    continue
                if not ok:
                    continue
                if codex is None and is_auth_paired(auth):
                    codex = _start_codex_session(
                        config,
                        auth,
                        runtime,
                        runtime_state,
                        metadata,
                        app_lock,
                        telegram,
                        handle_output,
                    )
                text = (update.get("message", {}).get("text") or "").strip()
                if text:
                    if codex is None:
                        telegram.send_message(auth.telegram_chat_id, "Codex is not ready yet.")
                        continue
                    codex.send(text)
                    recorder.record("telegram", text)
            time.sleep(config.poll_interval_seconds)
    finally:
        if codex is not None:
            codex.stop()
            runtime.stop_codex()
        recorder.stop()
        debug.stop()
        app_lock.clear()
        append_recovery_log(paths.recovery_log, "service stopped")


def isatty() -> bool:
    try:
        import sys

        return sys.stdin.isatty()
    except Exception:
        return False


def _start_codex_session(
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


def _handle_service_conflict(paths: AppPaths, app_lock: LockFile) -> None:
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
            if metadata.child_codex_pid and _is_owned_codex(metadata.child_codex_pid, metadata.cwd):
                safe_kill(metadata.child_codex_pid)
            app_lock.clear()
        elif choice == "exit":
            raise SystemExit(1)
    else:
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
                if orphan_choice == "kill" and _is_owned_codex(metadata.child_codex_pid, metadata.cwd):
                    safe_kill(metadata.child_codex_pid)
                elif orphan_choice == "exit":
                    raise SystemExit(1)
            app_lock.clear()
        elif choice == "exit":
            raise SystemExit(1)


def _is_owned_codex(pid: int, cwd: Optional[str]) -> bool:
    command = read_process_command(pid)
    if not command or "codex" not in command:
        return False
    if cwd and cwd not in command:
        return False
    return True
