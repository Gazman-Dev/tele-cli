from __future__ import annotations

import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

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


class ImmediateDeliveryManager:
    def __init__(self, target_getter):
        self._target_getter = target_getter

    @staticmethod
    def _call(func, *args, **kwargs):
        try:
            return func(*args, **kwargs)
        except TypeError:
            reduced = dict(kwargs)
            for key in ("disable_notification", "parse_mode", "topic_id", "caption"):
                reduced.pop(key, None)
                try:
                    return func(*args, **reduced)
                except TypeError:
                    continue
            raise

    def enqueue_and_wait(self, *, op_type: str, payload: dict, chat_id: int, topic_id: int | None = None, **metadata):
        target = self._target_getter()
        if target is None:
            raise RuntimeError("No Telegram test target is registered.")
        if op_type == "send_message":
            return self._call(
                target.send_message,
                chat_id,
                str(payload["text"]),
                topic_id=topic_id,
                parse_mode=payload.get("parse_mode"),
                disable_notification=bool(metadata.get("disable_notification")),
            )
        if op_type == "edit_message":
            return self._call(
                target.edit_message_text,
                chat_id,
                int(payload["message_id"]),
                str(payload["text"]),
                parse_mode=payload.get("parse_mode"),
            )
        if op_type == "delete_message":
            return target.delete_message(chat_id, int(payload["message_id"]))
        if op_type == "typing":
            return target.send_typing(chat_id, topic_id=topic_id)
        raise RuntimeError(f"Unsupported op_type {op_type!r} in test delivery manager.")


class TelegramHtmlFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.paths = build_paths(Path.cwd() / ".test_state" / "telegram_html_flow" / str(uuid.uuid4()))
        self.recorder = FakeRecorder()
        self._telegram_target = None
        self._delivery_manager = ImmediateDeliveryManager(lambda: self._telegram_target)
        original_init = FakeTelegramClient.__init__

        def registered_init(instance, *args, **kwargs):
            original_init(instance, *args, **kwargs)
            self._telegram_target = instance

        self._patches = [
            patch("runtime.performance.active_delivery_manager", return_value=self._delivery_manager),
            patch.object(FakeTelegramClient, "__init__", registered_init),
        ]
        for active_patch in self._patches:
            active_patch.start()

    def tearDown(self) -> None:
        for active_patch in reversed(getattr(self, "_patches", [])):
            active_patch.stop()

    def test_reasoning_update_sends_single_live_html_message(self) -> None:
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

        self.assertEqual(telegram.message_details, [(22, "Checking logs", None, "HTML", True)])
        refreshed = store.get_or_create_telegram_session(auth)
        self.assertEqual(refreshed.streaming_message_id, 1)
        self.assertEqual(refreshed.thinking_live_message_ids, {})

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
        self.assertIsNone(refreshed.streaming_message_id)

    def test_second_thinking_update_edits_existing_message_for_same_source(self) -> None:
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
                Notification("item/updated", {"threadId": "thread-1", "item": {"type": "reasoning", "text": "Checking logs"}}),
                Notification("item/updated", {"threadId": "thread-1", "item": {"type": "reasoning", "text": "Checking logs and config"}}),
            ]
        )

        drain_codex_notifications(self.paths, auth, telegram, self.recorder, codex)
        service_module._THINKING_SOURCE_LAST_SENT_AT.clear()
        maybe_refresh_thinking_message(self.paths, auth, telegram, store, recorder=self.recorder)

        self.assertEqual(telegram.messages, [(22, "Checking logs")])
        self.assertEqual(telegram.edits, [(22, 1, "Checking logs and config")])
        refreshed = store.get_or_create_telegram_session(auth)
        self.assertEqual(refreshed.streaming_message_id, 1)
        self.assertEqual(refreshed.thinking_live_texts.get("reasoning:current"), "Checking logs and config")

    def test_interleaved_commentary_and_command_render_in_one_live_message(self) -> None:
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

        self.assertEqual(telegram.messages, [(22, "Checking repo state")])
        self.assertEqual(
            telegram.edits,
            [
                (
                    22,
                    1,
                    "Checking repo state\n\n<pre><code class=\"language-bash\">git status --short</code></pre>",
                ),
                (
                    22,
                    1,
                    "Checking repo state and package config\n\n<pre><code class=\"language-bash\">git status --short</code></pre>",
                ),
            ],
        )
        refreshed = store.get_or_create_telegram_session(auth)
        self.assertEqual(refreshed.streaming_message_id, 1)
        self.assertEqual(refreshed.thinking_live_texts.get("commentary:msg-1"), "Checking repo state and package config")

    def test_final_reply_edits_live_message_with_collapsed_thinking_block(self) -> None:
        auth = AuthState(bot_token="token", telegram_user_id=11, telegram_chat_id=22, paired_at="now")
        store = SessionStore(self.paths)
        session = store.get_or_create_telegram_session(auth)
        session.thread_id = "thread-1"
        session.active_turn_id = "turn-1"
        session.status = "RUNNING_TURN"
        session.pending_output_text = "# Title\n**done**"
        session.streaming_message_id = 1
        session.thinking_history_order = ["commentary:msg-1", "command:cmd-1"]
        session.thinking_history_by_source = {
            "commentary:msg-1": "Checking repo",
            "command:cmd-1": "__tele_cli_command__:git status --short",
        }
        session.thinking_history_text = "Checking repo\n__tele_cli_command__:git status --short"
        store.save_session(session)
        telegram = FakeTelegramClient()

        flush_buffer(session.session_id, auth, telegram, self.recorder, store, mark_agent=True)

        self.assertEqual(telegram.deletes, [])
        self.assertEqual(telegram.messages, [])
        self.assertEqual(
            telegram.edits,
            [
                (
                    22,
                    1,
                    "<blockquote expandable>Checking repo\n\ngit status --short</blockquote>\n\n<b>Title</b>\n<b>done</b>",
                )
            ],
        )
        refreshed = store.get_or_create_telegram_session(auth)
        self.assertIsNone(refreshed.streaming_message_id)
        self.assertEqual(refreshed.thinking_live_texts, {})

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

    def test_live_thinking_uses_multiple_telegram_messages_when_over_limit(self) -> None:
        auth = AuthState(bot_token="token", telegram_user_id=11, telegram_chat_id=22, paired_at="now")
        store = SessionStore(self.paths)
        session = store.get_or_create_telegram_session(auth)
        session.thread_id = "thread-1"
        session.active_turn_id = "turn-1"
        session.status = "RUNNING_TURN"
        session.last_user_message_at = "2026-03-29T00:00:00+00:00"
        store.save_session(session)
        telegram = FakeTelegramClient()

        original_limit = service_module.TELEGRAM_TEXT_LIMIT
        try:
            service_module.TELEGRAM_TEXT_LIMIT = 40
            service_module.set_visible_thinking_message(
                auth,
                telegram,
                self.recorder,
                store,
                session,
                text="Alpha beta gamma delta epsilon zeta eta theta",
                source_key="commentary:msg-1",
            )
        finally:
            service_module.TELEGRAM_TEXT_LIMIT = original_limit

        self.assertEqual(len(telegram.messages), 2)
        refreshed = store.get_or_create_telegram_session(auth)
        self.assertEqual(refreshed.streaming_message_ids, [1, 2])
        self.assertEqual(refreshed.streaming_message_id, 1)

    def test_final_reply_uses_multiple_telegram_messages_when_over_limit(self) -> None:
        auth = AuthState(bot_token="token", telegram_user_id=11, telegram_chat_id=22, paired_at="now")
        store = SessionStore(self.paths)
        session = store.get_or_create_telegram_session(auth)
        session.thread_id = "thread-1"
        session.active_turn_id = "turn-1"
        session.status = "RUNNING_TURN"
        session.streaming_message_id = 1
        session.streaming_message_ids = [1]
        session.pending_output_text = "one two three four five six seven eight nine ten"
        session.thinking_history_order = ["commentary:msg-1"]
        session.thinking_history_by_source = {"commentary:msg-1": "checking logs and config"}
        session.thinking_history_text = "checking logs and config"
        store.save_session(session)
        telegram = FakeTelegramClient()

        original_limit = service_module.TELEGRAM_TEXT_LIMIT
        try:
            service_module.TELEGRAM_TEXT_LIMIT = 50
            flush_buffer(session.session_id, auth, telegram, self.recorder, store, mark_agent=True)
        finally:
            service_module.TELEGRAM_TEXT_LIMIT = original_limit

        self.assertTrue(telegram.edits)
        self.assertGreaterEqual(len(telegram.messages), 1)
        refreshed = store.get_or_create_telegram_session(auth)
        self.assertEqual(refreshed.streaming_message_ids, [])
        self.assertIsNone(refreshed.streaming_message_id)

    def test_collapsed_thinking_strips_code_blocks_and_stays_single_block(self) -> None:
        auth = AuthState(bot_token="token", telegram_user_id=11, telegram_chat_id=22, paired_at="now")
        store = SessionStore(self.paths)
        session = store.get_or_create_telegram_session(auth)
        session.thread_id = "thread-1"
        session.active_turn_id = "turn-1"
        session.status = "RUNNING_TURN"
        session.streaming_message_id = 1
        session.streaming_message_ids = [1]
        session.pending_output_text = "answer text " * 40
        session.thinking_history_order = ["commentary:msg-1", "command:cmd-1"]
        session.thinking_history_by_source = {
            "commentary:msg-1": "Checking [job.ts](/tmp/job.ts#L123)",
            "command:cmd-1": "__tele_cli_command__:git status --short",
        }
        session.thinking_history_text = "Checking [job.ts](/tmp/job.ts#L123)\n__tele_cli_command__:git status --short"
        store.save_session(session)
        telegram = FakeTelegramClient()

        original_limit = service_module.TELEGRAM_TEXT_LIMIT
        try:
            service_module.TELEGRAM_TEXT_LIMIT = 80
            flush_buffer(session.session_id, auth, telegram, self.recorder, store, mark_agent=True)
        finally:
            service_module.TELEGRAM_TEXT_LIMIT = original_limit

        rendered_texts = [text for _, _, text in telegram.edits] + [text for _, text in telegram.messages]
        combined = "\n".join(rendered_texts)
        self.assertEqual(combined.count("<blockquote expandable>"), 1)
        self.assertNotIn("<pre><code", combined)


if __name__ == "__main__":
    unittest.main()
