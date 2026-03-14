from __future__ import annotations

import time

from core.json_store import save_json
from core.logging_utils import append_recovery_log
from core.models import AuthState
from core.paths import AppPaths
from core.prompts import ask_text
from integrations.telegram import (
    TelegramClient,
    confirm_pairing_code,
    has_pending_pairing,
    is_auth_paired,
    register_pairing_request,
)


def pair_authorized_operator(paths: AppPaths, auth: AuthState, bot: TelegramClient) -> None:
    if has_pending_pairing(auth):
        print("Telegram pairing is pending from an earlier attempt.")
        if complete_pending_pairing(paths, auth, bot, allow_empty=True):
            return

    print("Telegram pairing setup has started.")
    print("Send any message to your bot from the Telegram chat that should control Tele Cli.")

    offset = None
    while not is_auth_paired(auth):
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
                if complete_pending_pairing(paths, auth, bot):
                    return
            if not ok:
                continue
        time.sleep(1)


def complete_pending_pairing(
    paths: AppPaths,
    auth: AuthState,
    bot: TelegramClient,
    allow_empty: bool = False,
) -> bool:
    while not is_auth_paired(auth):
        if not has_pending_pairing(auth):
            return False
        prompt = "Enter the pairing code shown by the Telegram bot"
        if allow_empty:
            prompt = f"{prompt} (press Enter to skip)"
        code = ask_text(prompt)
        if allow_empty and not code.strip():
            return False
        if confirm_pairing_code(auth, code):
            save_json(paths.auth, auth.to_dict())
            bot.send_message(auth.telegram_chat_id, "Pairing complete. Tele Cli is now authorized for this chat.")
            append_recovery_log(
                paths.recovery_log,
                f"telegram paired chat_id={auth.telegram_chat_id} user_id={auth.telegram_user_id}",
            )
            return True
        print("Invalid pairing code. Enter the current code from Telegram.")
    return True
