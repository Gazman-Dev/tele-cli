from __future__ import annotations


class FakeTelegramClient:
    def __init__(self, updates: list[dict] | None = None):
        self._updates = list(updates or [])
        self.messages: list[tuple[int, str]] = []
        self.edits: list[tuple[int, int, str]] = []
        self.typing_actions: list[int] = []

    def get_updates(self, offset=None, timeout: int = 20) -> list[dict]:
        return list(self._updates)

    def send_message(self, chat_id: int, text: str, topic_id: int | None = None) -> dict:
        if topic_id is None:
            self.messages.append((chat_id, text))
        else:
            self.messages.append((chat_id, text, topic_id))
        return {"message_id": len(self.messages)}

    def edit_message_text(self, chat_id: int, message_id: int, text: str) -> dict:
        self.edits.append((chat_id, message_id, text))
        return {"message_id": message_id}

    def send_typing(self, chat_id: int, topic_id: int | None = None) -> None:
        if topic_id is None:
            self.typing_actions.append(chat_id)
        else:
            self.typing_actions.append((chat_id, topic_id))
