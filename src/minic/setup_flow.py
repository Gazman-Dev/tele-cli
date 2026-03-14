from __future__ import annotations

import os
import shutil
import time
from typing import Optional

from . import APP_VERSION
from .installer import current_installer
from .json_store import load_json, save_json
from .locks import LockFile
from .logging_utils import append_recovery_log
from .models import AuthState, Config, SetupState, utc_now
from .paths import AppPaths
from .process import describe_process, make_lock_metadata, process_exists, safe_kill
from .prompts import ask_choice, ask_text
from .telegram import TelegramClient, confirm_pairing_code, register_pairing_request


def load_setup_state(paths: AppPaths) -> Optional[SetupState]:
    return load_json(paths.setup_lock, SetupState.from_dict)


def save_setup_state(paths: AppPaths, state: SetupState) -> None:
    save_json(paths.setup_lock, state.to_dict())


def run_setup(paths: AppPaths) -> None:
    paths.root.mkdir(parents=True, exist_ok=True)
    app_lock = LockFile(paths.app_lock)
    _handle_existing_app_lock(app_lock, paths)
    app_lock.write(make_lock_metadata(mode="setup", app_version=APP_VERSION, cwd=paths.root))
    setup_state = _handle_existing_setup(paths)
    setup_state.pid = os.getpid()
    save_setup_state(paths, setup_state)
    installer = current_installer()
    config = load_json(paths.config, Config.from_dict) or Config(state_dir=str(paths.root))
    existing_auth = load_json(paths.auth, AuthState.from_dict)
    try:
        if shutil.which("npm"):
            setup_state.npm_installed = True
        else:
            plan = installer.install_npm(allow_homebrew_install=config.install_homebrew_if_missing)
            print(f"Installing npm via {plan.manager}: {' '.join(plan.command)}")
            installer.run(plan)
            setup_state.npm_installed = True
            save_setup_state(paths, setup_state)

        if shutil.which("codex"):
            setup_state.codex_installed = True
        else:
            plan = installer.install_codex()
            print(f"Installing Codex: {' '.join(plan.command)}")
            installer.run(plan)
            setup_state.codex_installed = True
            save_setup_state(paths, setup_state)

        if existing_auth and existing_auth.bot_token:
            auth = existing_auth
            print("Reusing saved Telegram bot token from a previous setup attempt.")
        else:
            token = ask_text("Telegram bot token", secret=True)
            auth = AuthState(bot_token=token)
            save_json(paths.auth, auth.to_dict())
            setup_state.telegram_token_saved = True
            save_setup_state(paths, setup_state)

        bot = TelegramClient(auth.bot_token)
        bot.validate()
        setup_state.telegram_validated = True
        save_setup_state(paths, setup_state)

        if auth.telegram_chat_id and auth.telegram_user_id:
            print("Telegram chat is already paired. Keeping existing authorization.")
        else:
            _pair_authorized_operator(paths, auth, bot)
        setup_state.status = "completed"
        save_setup_state(paths, setup_state)
        save_json(paths.config, config.to_dict())
        append_recovery_log(paths.recovery_log, "setup completed")
        app_lock.clear()
        print("Setup complete.")
    except Exception:
        setup_state.status = "failed"
        save_setup_state(paths, setup_state)
        append_recovery_log(paths.recovery_log, "setup failed")
        app_lock.clear()
        raise


def _pair_authorized_operator(paths: AppPaths, auth: AuthState, bot: TelegramClient) -> None:
    print("Telegram pairing setup has started.")
    print("Send any message to your bot from the Telegram chat that should control Tele Cli.")

    offset = None
    while auth.telegram_chat_id is None or auth.telegram_user_id is None:
        updates = bot.get_updates(offset=offset, timeout=5)
        for update in updates:
            offset = update["update_id"] + 1
            ok, status = register_pairing_request(auth, update)
            save_json(paths.auth, auth.to_dict())
            if status == "already-paired":
                chat_id = update.get("message", {}).get("chat", {}).get("id")
                if chat_id:
                    bot.send_message(chat_id, "This bot is already paired to another chat.")
                continue
            if status == "authorized":
                return
            if status == "code-issued" and auth.pending_chat_id and auth.pairing_code:
                bot.send_message(
                    auth.pending_chat_id,
                    f"Pairing code: {auth.pairing_code}. Enter this code in the local Tele Cli setup terminal.",
                )
                print(
                    "Pairing request received. "
                    f"chat_id={auth.pending_chat_id} user_id={auth.pending_user_id}"
                )
                _wait_for_local_pairing_confirmation(paths, auth, bot)
                return

        time.sleep(1)


def _wait_for_local_pairing_confirmation(paths: AppPaths, auth: AuthState, bot: TelegramClient) -> None:
    while auth.telegram_chat_id is None or auth.telegram_user_id is None:
        code = ask_text("Enter the pairing code shown by the Telegram bot")
        if confirm_pairing_code(auth, code):
            save_json(paths.auth, auth.to_dict())
            bot.send_message(auth.telegram_chat_id, "Pairing complete. Tele Cli is now authorized for this chat.")
            append_recovery_log(
                paths.recovery_log,
                f"telegram paired chat_id={auth.telegram_chat_id} user_id={auth.telegram_user_id}",
            )
            return
        print("Invalid pairing code. Enter the current code from Telegram.")


def _handle_existing_app_lock(app_lock: LockFile, paths: AppPaths) -> None:
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


def _handle_existing_setup(paths: AppPaths) -> SetupState:
    existing = load_setup_state(paths)
    if not existing:
        state = SetupState(status="started", pid=0, timestamp=utc_now())
        save_setup_state(paths, state)
        return state

    if existing.status == "completed":
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
