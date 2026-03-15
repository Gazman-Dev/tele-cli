from __future__ import annotations


class FakeTelegramClient:
    def __init__(self, updates: list[dict] | None = None):
        self._updates = list(updates or [])
        self.messages: list[tuple[int, str]] = []

    def get_updates(self, offset=None, timeout: int = 20) -> list[dict]:
        return list(self._updates)

    def send_message(self, chat_id: int, text: str) -> None:
        self.messages.append((chat_id, text))
