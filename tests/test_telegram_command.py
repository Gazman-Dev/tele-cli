from __future__ import annotations

import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

from core.json_store import save_json
from core.models import AuthState
from core.paths import build_paths
from telegram_command import resolve_telegram_session, run_telegram_command


class TelegramCommandTests(unittest.TestCase):
    def test_resolve_telegram_session_supports_main_and_topic_syntax(self) -> None:
        auth = AuthState(bot_token="token", telegram_user_id=11, telegram_chat_id=22, paired_at="now")
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))

            self.assertEqual(resolve_telegram_session(paths, auth, "main"), (22, None))
            self.assertEqual(resolve_telegram_session(paths, auth, "-100123/77"), (-100123, 77))
            self.assertEqual(resolve_telegram_session(paths, auth, "chat:-100123/topic:77"), (-100123, 77))

    def test_resolve_telegram_session_supports_current_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            auth = AuthState(bot_token="token", telegram_user_id=11, telegram_chat_id=22, paired_at="now")
            from runtime.session_store import SessionStore

            session = SessionStore(paths).get_or_create_telegram_session(auth, topic_id=77)
            session.last_user_message_at = "2026-03-27T12:00:00+00:00"
            SessionStore(paths).save_session(session)

            self.assertEqual(resolve_telegram_session(paths, auth, "current"), (22, 77))

    def test_run_telegram_command_sends_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            save_json(paths.auth, AuthState(bot_token="token", telegram_user_id=11, telegram_chat_id=22, paired_at="now").to_dict())

            class FakeTelegram:
                def __init__(self, token: str):
                    self.messages: list[tuple[int, str, int | None]] = []

                def send_message(self, chat_id: int, text: str, topic_id: int | None = None, parse_mode: str | None = None):
                    self.messages.append((chat_id, text, topic_id))
                    return {"message_id": 1}

            fake = FakeTelegram("token")
            with patch("telegram_command.TelegramClient", return_value=fake):
                run_telegram_command(paths, Namespace(session_name="main", telegram_target="message", text="hello"))

            self.assertEqual(fake.messages, [(22, "hello", None)])

    def test_run_telegram_command_sends_photo_and_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            save_json(paths.auth, AuthState(bot_token="token", telegram_user_id=11, telegram_chat_id=22, paired_at="now").to_dict())
            image_path = Path(tmp) / "image.png"
            image_path.write_bytes(b"png")
            file_path = Path(tmp) / "notes.txt"
            file_path.write_text("hello", encoding="utf-8")

            class FakeTelegram:
                def __init__(self, token: str):
                    self.photos: list[tuple[int, str, str | None]] = []
                    self.documents: list[tuple[int, str, str | None]] = []

                def send_photo(self, chat_id: int, photo_path, *, topic_id: int | None = None, caption: str | None = None, parse_mode: str | None = None):
                    self.photos.append((chat_id, str(photo_path), caption))
                    return {"message_id": 1}

                def send_document(self, chat_id: int, document_path, *, topic_id: int | None = None, caption: str | None = None, parse_mode: str | None = None):
                    self.documents.append((chat_id, str(document_path), caption))
                    return {"message_id": 2}

            fake = FakeTelegram("token")
            with patch("telegram_command.TelegramClient", return_value=fake):
                run_telegram_command(
                    paths,
                    Namespace(session_name="main", telegram_target="image", path=str(image_path), caption="look"),
                )
                run_telegram_command(
                    paths,
                    Namespace(session_name="-100123/77", telegram_target="file", path=str(file_path), caption="report"),
                )

            self.assertEqual(fake.photos, [(22, str(image_path.resolve()), "look")])
            self.assertEqual(fake.documents, [(-100123, str(file_path.resolve()), "report")])


if __name__ == "__main__":
    unittest.main()
