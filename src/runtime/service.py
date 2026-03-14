from __future__ import annotations

import time
import uuid

from .debug_mirror import DebugMirror
from core.json_store import load_json, save_json
from core.logging_utils import append_recovery_log
from core.models import AuthState, Config, RuntimeState, utc_now
from core.paths import AppPaths
from integrations.telegram import TelegramClient, has_pending_pairing, is_auth_paired, register_pairing_request
from setup.setup_flow import complete_pending_pairing
from .control import isatty, prepare_service_lock, reset_auth, start_codex_session
from .recorder import Recorder
from .runtime import ServiceRuntime


def run_service(paths: AppPaths) -> None:
    paths.root.mkdir(parents=True, exist_ok=True)
    config = load_json(paths.config, Config.from_dict)
    auth = load_json(paths.auth, AuthState.from_dict)
    if not config or not auth:
        raise RuntimeError("Run setup first.")

    app_lock, metadata = prepare_service_lock(paths)

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
        codex = start_codex_session(config, auth, runtime, runtime_state, metadata, app_lock, telegram, handle_output)
    save_json(paths.runtime, runtime_state.to_dict())
    append_recovery_log(paths.recovery_log, f"service started session_id={runtime_state.session_id}")

    offset = None
    try:
        if has_pending_pairing(auth) and isatty():
            complete_pending_pairing(paths, auth, telegram, allow_empty=True)
            if is_auth_paired(auth) and codex is None:
                codex = start_codex_session(
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
                            codex = start_codex_session(
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
                    codex = start_codex_session(
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
