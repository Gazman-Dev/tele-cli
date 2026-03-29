from __future__ import annotations

import tempfile
import unittest
import uuid
from pathlib import Path

from core.models import AuthState
from core.paths import build_paths
from integrations.telegram import TelegramError
from runtime import service as service_module
from runtime.service import drain_codex_notifications, flush_buffer, maybe_refresh_thinking_message
from runtime.session_store import SessionStore
from tests.fakes.fake_telegram import FakeTelegramClient


class FakeRecorder:
    def __init__(self) -> None:
        self.records: list[tuple[str, str]] = []

    def record(self, source: str, line: str) -> None:
        self.records.append((source, line))


class Notification:
    def __init__(self, method: str, params: dict):
        self.method = method
        self.params = params


class FakeCodex:
    def __init__(self) -> None:
        self.pending_notifications: list[Notification] = []

    def poll_notification(self):
        if self.pending_notifications:
            return self.pending_notifications.pop(0)
        return None


class TelegramHtmlFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.paths = build_paths(Path.cwd() / ".test_state" / "telegram_html_flow" / str(uuid.uuid4()))
        self.recorder = FakeRecorder()

    def test_reasoning_update_sends_independent_html_message(self) -> None:
        auth = AuthState(bot_token="token", telegram_user_id=11, telegram_chat_id=22, paired_at="now")
        store = SessionStore(self.paths)
        session = store.get_or_create_telegram_session(auth)
        session.thread_id = "thread-1"
        session.active_turn_id = "turn-1"
        session.status = "RUNNING_TURN"
        session.last_user_message_at = "2026-03-29T00:00:00+00:00"
        store.save_session(session)
        telegram = FakeTelegramClient()
        codex = FakeCodex()
        codex.pending_notifications.append(
            Notification("item/updated", {"threadId": "thread-1", "item": {"type": "reasoning", "text": "Checking logs"}})
        )

        drain_codex_notifications(self.paths, auth, telegram, self.recorder, codex)

        self.assertEqual(telegram.message_details, [(22, "<b>Thinking</b>\n\nChecking logs", None, "HTML", True)])
        refreshed = store.get_or_create_telegram_session(auth)
        self.assertEqual(refreshed.thinking_message_id, 1)
        self.assertEqual(refreshed.thinking_message_ids, [1])
        self.assertEqual(refreshed.thinking_live_message_ids, {"reasoning:current": 1})

    def test_short_token_delta_does_not_send_live_thinking_message(self) -> None:
        auth = AuthState(bot_token="token", telegram_user_id=11, telegram_chat_id=22, paired_at="now")
        store = SessionStore(self.paths)
        session = store.get_or_create_telegram_session(auth)
        session.thread_id = "thread-1"
        session.active_turn_id = "turn-1"
        session.status = "RUNNING_TURN"
        session.last_user_message_at = "2026-03-29T00:00:00+00:00"
        store.save_session(session)
        telegram = FakeTelegramClient()
        codex = FakeCodex()
        codex.pending_notifications.extend(
            [
                Notification(
                    "item/started",
                    {"threadId": "thread-1", "turnId": "turn-1", "item": {"id": "msg-1", "type": "agentMessage", "phase": "commentary", "text": ""}},
                ),
                Notification("item/agentMessage/delta", {"threadId": "thread-1", "turnId": "turn-1", "itemId": "msg-1", "delta": "I"}),
            ]
        )

        drain_codex_notifications(self.paths, auth, telegram, self.recorder, codex)

        self.assertEqual(telegram.messages, [])
        refreshed = store.get_or_create_telegram_session(auth)
        self.assertEqual(refreshed.thinking_message_text, "I")
        self.assertIsNone(refreshed.thinking_message_id)

    def test_second_thinking_update_edits_existing_message_for_same_source(self) -> None:
        auth = AuthState(bot_token="token", telegram_user_id=11, telegram_chat_id=22, paired_at="now")
        store = SessionStore(self.paths)
        session = store.get_or_create_telegram_session(auth)
        session.thread_id = "thread-1"
        session.active_turn_id = "turn-1"
        session.status = "RUNNING_TURN"
        store.save_session(session)
        telegram = FakeTelegramClient()
        codex = FakeCodex()
        codex.pending_notifications.extend(
            [
                Notification("item/updated", {"threadId": "thread-1", "item": {"type": "reasoning", "text": "Checking logs"}}),
                Notification("item/updated", {"threadId": "thread-1", "item": {"type": "reasoning", "text": "Checking logs and config"}}),
            ]
        )

        drain_codex_notifications(self.paths, auth, telegram, self.recorder, codex)
        service_module._THINKING_SOURCE_LAST_SENT_AT.clear()
        maybe_refresh_thinking_message(self.paths, auth, telegram, store, recorder=self.recorder)

        self.assertEqual(telegram.messages, [(22, "<b>Thinking</b>\n\nChecking logs")])
        self.assertEqual(telegram.edits, [])
        refreshed = store.get_or_create_telegram_session(auth)
        self.assertEqual(refreshed.thinking_message_ids, [1])
        self.assertEqual(refreshed.thinking_live_texts.get("reasoning:current"), "Checking logs and config")

    def test_interleaved_commentary_and_command_keep_separate_live_messages(self) -> None:
        auth = AuthState(bot_token="token", telegram_user_id=11, telegram_chat_id=22, paired_at="now")
        store = SessionStore(self.paths)
        session = store.get_or_create_telegram_session(auth)
        session.thread_id = "thread-1"
        session.active_turn_id = "turn-1"
        session.status = "RUNNING_TURN"
        store.save_session(session)
        telegram = FakeTelegramClient()
        codex = FakeCodex()
        codex.pending_notifications.extend(
            [
                Notification(
                    "item/started",
                    {"threadId": "thread-1", "turnId": "turn-1", "item": {"id": "msg-1", "type": "agentMessage", "phase": "commentary", "text": ""}},
                ),
                Notification("item/agentMessage/delta", {"threadId": "thread-1", "turnId": "turn-1", "itemId": "msg-1", "delta": "Checking repo state"}),
                Notification(
                    "item/started",
                    {"threadId": "thread-1", "turnId": "turn-1", "item": {"id": "cmd-1", "type": "commandExecution", "command": "git status --short"}},
                ),
                Notification("item/agentMessage/delta", {"threadId": "thread-1", "turnId": "turn-1", "itemId": "msg-1", "delta": " and package config"}),
            ]
        )

        drain_codex_notifications(self.paths, auth, telegram, self.recorder, codex)
        service_module._THINKING_SOURCE_LAST_SENT_AT.clear()
        maybe_refresh_thinking_message(self.paths, auth, telegram, store, recorder=self.recorder)

        self.assertEqual(
            telegram.messages,
            [
                (22, "<b>Thinking</b>\n\nChecking repo state"),
                (22, '<b>Running</b>\n\n<pre><code class="language-bash">git status --short</code></pre>'),
            ],
        )
        self.assertEqual(telegram.edits, [])
        refreshed = store.get_or_create_telegram_session(auth)
        self.assertEqual(set(refreshed.thinking_live_message_ids.keys()), {"commentary:msg-1", "command:cmd-1"})
        self.assertEqual(refreshed.thinking_live_texts.get("commentary:msg-1"), "Checking repo state and package config")

    def test_final_reply_includes_collapsed_thinking_block(self) -> None:
        auth = AuthState(bot_token="token", telegram_user_id=11, telegram_chat_id=22, paired_at="now")
        store = SessionStore(self.paths)
        session = store.get_or_create_telegram_session(auth)
        session.thread_id = "thread-1"
        session.active_turn_id = "turn-1"
        session.status = "RUNNING_TURN"
        session.pending_output_text = "# Title\n**done**"
        session.thinking_message_id = 2
        session.thinking_message_ids = [1, 2]
        session.thinking_live_message_ids = {"commentary:msg-1": 1, "command:cmd-1": 2}
        session.thinking_history_order = ["commentary:msg-1", "command:cmd-1"]
        session.thinking_history_by_source = {
            "commentary:msg-1": "Checking repo",
            "command:cmd-1": "__tele_cli_command__:git status --short",
        }
        session.thinking_history_text = "Checking repo\n__tele_cli_command__:git status --short"
        store.save_session(session)
        telegram = FakeTelegramClient()

        flush_buffer(session.session_id, auth, telegram, self.recorder, store, mark_agent=True)

        self.assertEqual(telegram.deletes, [(22, 1), (22, 2)])
        self.assertEqual(
            telegram.messages,
            [
                (
                    22,
                    "<b>Thinking</b>\n<blockquote expandable>Thinking\n\nChecking repo\n\nRunning\n\ngit status --short</blockquote>",
                ),
                (22, "<b>Title</b>\n<b>done</b>"),
            ],
        )
        refreshed = store.get_or_create_telegram_session(auth)
        self.assertEqual(refreshed.thinking_message_ids, [])
        self.assertEqual(refreshed.thinking_live_message_ids, {})

    def test_final_reply_falls_back_to_escaped_html(self) -> None:
        auth = AuthState(bot_token="token", telegram_user_id=11, telegram_chat_id=22, paired_at="now")
        store = SessionStore(self.paths)
        session = store.get_or_create_telegram_session(auth)
        session.thread_id = "thread-1"
        session.active_turn_id = "turn-1"
        session.status = "RUNNING_TURN"
        session.pending_output_text = "# Title\n**done**"
        store.save_session(session)

        class FailingTelegram(FakeTelegramClient):
            def __init__(self) -> None:
                super().__init__()
                self.parse_modes: list[str | None] = []

            def send_message(
                self,
                chat_id: int,
                text: str,
                topic_id: int | None = None,
                parse_mode: str | None = None,
                disable_notification: bool = False,
            ) -> dict:
                self.parse_modes.append(parse_mode)
                if parse_mode == "HTML" and text == "<b>Title</b>\n<b>done</b>":
                    raise TelegramError("can't parse entities")
                return super().send_message(
                    chat_id,
                    text,
                    topic_id=topic_id,
                    parse_mode=parse_mode,
                    disable_notification=disable_notification,
                )

        telegram = FailingTelegram()
        flush_buffer(session.session_id, auth, telegram, self.recorder, store, mark_agent=True)

        self.assertEqual(telegram.parse_modes, ["HTML", "HTML"])
        self.assertEqual(telegram.messages, [(22, "# Title\n**done**")])


if __name__ == "__main__":
    unittest.main()
