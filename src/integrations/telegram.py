from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

from core.models import AuthState, utc_now


class TelegramError(RuntimeError):
    pass


class TelegramClient:
    def __init__(self, token: str):
        self.token = token
        self.base_url = f"https://api.telegram.org/bot{token}"

    def _request(self, method: str, params: Optional[dict] = None) -> dict:
        url = f"{self.base_url}/{method}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        try:
            with urllib.request.urlopen(url, timeout=30) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.URLError as exc:
            raise TelegramError(str(exc)) from exc
        if not payload.get("ok"):
            raise TelegramError(str(payload))
        return payload["result"]

    def validate(self) -> dict:
        return self._request("getMe")

    def get_updates(self, offset: Optional[int] = None, timeout: int = 20) -> list[dict]:
        params = {"timeout": timeout}
        if offset is not None:
            params["offset"] = offset
        return self._request("getUpdates", params=params)

    def send_message(self, chat_id: int, text: str, topic_id: int | None = None) -> dict:
        params = {"chat_id": chat_id, "text": text}
        if topic_id is not None:
            params["message_thread_id"] = topic_id
        return self._request("sendMessage", params=params)

    def edit_message_text(self, chat_id: int, message_id: int, text: str) -> dict:
        return self._request("editMessageText", params={"chat_id": chat_id, "message_id": message_id, "text": text})

    def send_typing(self, chat_id: int, topic_id: int | None = None) -> None:
        params = {"chat_id": chat_id, "action": "typing"}
        if topic_id is not None:
            params["message_thread_id"] = topic_id
        self._request("sendChatAction", params=params)


def is_auth_paired(auth: Optional[AuthState]) -> bool:
    if not auth or not auth.bot_token:
        return False
    return bool(auth.telegram_user_id and auth.paired_at)


def has_pending_pairing(auth: Optional[AuthState]) -> bool:
    if not auth:
        return False
    return bool(auth.pairing_code and auth.pending_chat_id and auth.pending_user_id)


def describe_pairing(auth: Optional[AuthState]) -> str:
    if not auth or not auth.bot_token:
        return "missing bot token"
    if is_auth_paired(auth):
        if auth.telegram_chat_id:
            return f"paired user={auth.telegram_user_id} default_chat={auth.telegram_chat_id}"
        return f"paired user={auth.telegram_user_id}"
    if has_pending_pairing(auth):
        return f"pending code for chat={auth.pending_chat_id} user={auth.pending_user_id}"
    return "not paired"


def issue_pairing_code(auth: AuthState) -> str:
    import random

    auth.pairing_code = f"{random.randint(0, 99999):05d}"
    auth.pending_issued_at = utc_now()
    return auth.pairing_code


def register_pairing_request(auth: AuthState, update: dict) -> tuple[bool, str]:
    message = update.get("message") or {}
    from_user = message.get("from") or {}
    chat = message.get("chat") or {}
    user_id = from_user.get("id")
    chat_id = chat.get("id")
    if is_auth_paired(auth):
        if auth.telegram_user_id != user_id:
            return False, "already-paired"
        return True, "authorized"

    if not user_id or not chat_id:
        return False, "pending"

    if auth.pending_chat_id is None or auth.pending_user_id is None:
        issue_pairing_code(auth)
        auth.pending_chat_id = int(chat_id)
        auth.pending_user_id = int(user_id)
        return False, "code-issued"

    if auth.pending_chat_id != int(chat_id) or auth.pending_user_id != int(user_id):
        issue_pairing_code(auth)
        auth.pending_chat_id = int(chat_id)
        auth.pending_user_id = int(user_id)
        return False, "code-issued"

    return False, "code-issued"


def confirm_pairing_code(auth: AuthState, code: str) -> bool:
    if not auth.pairing_code or not auth.pending_chat_id or not auth.pending_user_id:
        return False
    if code.strip() != auth.pairing_code:
        return False

    auth.telegram_user_id = auth.pending_user_id
    auth.telegram_chat_id = auth.pending_chat_id
    auth.paired_at = utc_now()
    auth.pairing_code = None
    auth.pending_chat_id = None
    auth.pending_user_id = None
    auth.pending_issued_at = None
    return True
