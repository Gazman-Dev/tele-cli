from __future__ import annotations


class FakeTelegramClient:
    def __init__(self, updates: list[dict] | None = None):
        self._updates = list(updates or [])
        self.messages: list[tuple[int, str]] = []
        self.edits: list[tuple[int, int, str]] = []
        self.typing_actions: list[int] = []

    def get_updates(self, offset=None, timeout: int = 20) -> list[dict]:
        return list(self._updates)

    def send_message(self, chat_id: int, text: str) -> dict:
        self.messages.append((chat_id, text))
        return {"message_id": len(self.messages)}

    def edit_message_text(self, chat_id: int, message_id: int, text: str) -> dict:
        self.edits.append((chat_id, message_id, text))
        return {"message_id": message_id}

    def send_typing(self, chat_id: int) -> None:
        self.typing_actions.append(chat_id)
