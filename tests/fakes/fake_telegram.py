from __future__ import annotations


class FakeTelegramClient:
    def __init__(self, updates: list[dict] | None = None):
        self._updates = list(updates or [])
        self.messages: list[tuple[int, str]] = []
        self.message_details: list[tuple[int, str, int | None, str | None, bool]] = []
        self.edits: list[tuple[int, int, str]] = []
        self.deletes: list[tuple[int, int]] = []
        self.typing_actions: list[int] = []
        self.photos: list[tuple[int, str, str | None]] = []
        self.documents: list[tuple[int, str, str | None]] = []
        self.files: dict[str, dict] = {}
        self.downloads: dict[str, bytes] = {}

    def get_updates(self, offset=None, timeout: int = 20) -> list[dict]:
        return list(self._updates)

    def send_message(
        self,
        chat_id: int,
        text: str,
        topic_id: int | None = None,
        parse_mode: str | None = None,
        disable_notification: bool = False,
    ) -> dict:
        self.message_details.append((chat_id, text, topic_id, parse_mode, disable_notification))
        if topic_id is None:
            self.messages.append((chat_id, text))
        else:
            self.messages.append((chat_id, text, topic_id))
        return {"message_id": len(self.messages)}

    def edit_message_text(self, chat_id: int, message_id: int, text: str, parse_mode: str | None = None) -> dict:
        self.edits.append((chat_id, message_id, text))
        return {"message_id": message_id}

    def delete_message(self, chat_id: int, message_id: int) -> dict:
        self.deletes.append((chat_id, message_id))
        return {"ok": True}

    def send_photo(
        self,
        chat_id: int,
        photo_path,
        *,
        topic_id: int | None = None,
        caption: str | None = None,
        parse_mode: str | None = None,
    ) -> dict:
        self.photos.append((chat_id, str(photo_path), caption))
        return {"message_id": len(self.photos)}

    def send_document(
        self,
        chat_id: int,
        document_path,
        *,
        topic_id: int | None = None,
        caption: str | None = None,
        parse_mode: str | None = None,
    ) -> dict:
        self.documents.append((chat_id, str(document_path), caption))
        return {"message_id": len(self.documents)}

    def get_file(self, file_id: str) -> dict:
        return dict(self.files[file_id])

    def download_file(self, file_path: str) -> bytes:
        return self.downloads[file_path]

    def send_typing(self, chat_id: int, topic_id: int | None = None) -> None:
        if topic_id is None:
            self.typing_actions.append(chat_id)
        else:
            self.typing_actions.append((chat_id, topic_id))
