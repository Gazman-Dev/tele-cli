from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

from .models import AuthState, utc_now


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

    def send_message(self, chat_id: int, text: str) -> None:
        self._request("sendMessage", params={"chat_id": chat_id, "text": text})


def issue_pairing_code(auth: AuthState) -> str:
    import random

    auth.pairing_code = f"{random.randint(0, 99999):05d}"
    auth.pending_issued_at = utc_now()
    return auth.pairing_code


def pair_from_update(auth: AuthState, update: dict) -> tuple[bool, str]:
    message = update.get("message") or {}
    from_user = message.get("from") or {}
    chat = message.get("chat") or {}
    text = (message.get("text") or "").strip()
    user_id = from_user.get("id")
    chat_id = chat.get("id")
    if auth.telegram_chat_id and auth.telegram_user_id:
        if auth.telegram_chat_id != chat_id or auth.telegram_user_id != user_id:
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

    if text == auth.pairing_code:
        auth.telegram_user_id = int(user_id)
        auth.telegram_chat_id = int(chat_id)
        auth.paired_at = utc_now()
        auth.pairing_code = None
        auth.pending_chat_id = None
        auth.pending_user_id = None
        auth.pending_issued_at = None
        return True, "paired"
    return False, "pending"
