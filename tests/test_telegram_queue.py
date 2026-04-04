from __future__ import annotations

from contextlib import closing
import json
import sqlite3
import time
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core.models import AuthState
from core.paths import build_paths
from integrations.telegram import TelegramError
from runtime import service as service_module  # Preload runtime/storage graph before importing the queue module.
from runtime.session_store import SessionStore
from storage.operations import ServiceRunStore
from storage.telegram_queue import TelegramDeliveryManager
from tests.fakes.fake_telegram import FakeTelegramClient


_RATE_LIMIT_STATE_KEY = "telegram_delivery_backoff"


class RateLimitedTelegram(FakeTelegramClient):
    def __init__(self, *, edit_failures: int = 0) -> None:
        super().__init__()
        self._remaining_edit_failures = edit_failures

    def edit_message_text(self, chat_id: int, message_id: int, text: str, parse_mode: str | None = None) -> dict:
        if self._remaining_edit_failures > 0:
            self._remaining_edit_failures -= 1
            raise TelegramError("{'ok': False, 'error_code': 429, 'parameters': {'retry_after': 1}}")
        return super().edit_message_text(chat_id, message_id, text, parse_mode=parse_mode)


class BareHttp429Telegram(FakeTelegramClient):
    def edit_message_text(self, chat_id: int, message_id: int, text: str, parse_mode: str | None = None) -> dict:
        raise TelegramError("HTTP Error 429: Too Many Requests")


class SendRateLimitedTelegram(FakeTelegramClient):
    def __init__(self, *, send_failures: int = 0) -> None:
        super().__init__()
        self._remaining_send_failures = send_failures

    def send_message(
        self,
        chat_id: int,
        text: str,
        topic_id: int | None = None,
        parse_mode: str | None = None,
        disable_notification: bool = False,
    ) -> dict:
        if self._remaining_send_failures > 0:
            self._remaining_send_failures -= 1
            raise TelegramError("{'ok': False, 'error_code': 429, 'parameters': {'retry_after': 1}}")
        return super().send_message(
            chat_id,
            text,
            topic_id=topic_id,
            parse_mode=parse_mode,
            disable_notification=disable_notification,
        )


class CallbackTelegram(FakeTelegramClient):
    def __init__(self, on_send=None) -> None:
        super().__init__()
        self._on_send = on_send

    def send_message(
        self,
        chat_id: int,
        text: str,
        topic_id: int | None = None,
        parse_mode: str | None = None,
        disable_notification: bool = False,
    ) -> dict:
        if self._on_send is not None:
            callback = self._on_send
            self._on_send = None
            callback()
        return super().send_message(
            chat_id,
            text,
            topic_id=topic_id,
            parse_mode=parse_mode,
            disable_notification=disable_notification,
        )


class TelegramQueueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.paths = build_paths(Path.cwd() / ".test_state" / "telegram_queue" / str(uuid.uuid4()))

    def _read_app_state(self, key: str) -> dict:
        with closing(sqlite3.connect(self.paths.database)) as connection:
            row = connection.execute("SELECT value_json FROM app_state WHERE state_key = ?", (key,)).fetchone()
        if row is None:
            return {}
        return json.loads(str(row[0]))

    def _set_pause_state(self, *, paused_until: datetime, backoff_seconds: int) -> None:
        with closing(sqlite3.connect(self.paths.database)) as connection:
            connection.execute(
                """
                INSERT INTO app_state(state_key, value_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(state_key) DO UPDATE SET
                    value_json = excluded.value_json,
                    updated_at = excluded.updated_at
                """,
                (
                    _RATE_LIMIT_STATE_KEY,
                    json.dumps(
                        {
                            "paused_until": paused_until.astimezone(timezone.utc).isoformat(),
                            "backoff_seconds": backoff_seconds,
                        }
                    ),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            connection.commit()

    def _expire_pause_and_queue(self, *, backoff_seconds: int = 0) -> None:
        expired = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        with closing(sqlite3.connect(self.paths.database)) as connection:
            connection.execute(
                """
                INSERT INTO app_state(state_key, value_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(state_key) DO UPDATE SET
                    value_json = excluded.value_json,
                    updated_at = excluded.updated_at
                """,
                (
                    _RATE_LIMIT_STATE_KEY,
                    json.dumps({"paused_until": expired, "backoff_seconds": backoff_seconds}),
                    expired,
                ),
            )
            connection.execute(
                "UPDATE telegram_outbound_queue SET available_at = ? WHERE status = 'queued'",
                (expired,),
            )
            connection.commit()

    def test_paused_replaceable_send_keeps_only_latest_payload(self) -> None:
        telegram = FakeTelegramClient()
        manager = TelegramDeliveryManager(self.paths, telegram, run_id="run-1")
        ServiceRunStore(self.paths).start(run_id="run-1", pid=1)
        paused_until = datetime.now(timezone.utc) + timedelta(minutes=5)
        self._set_pause_state(paused_until=paused_until, backoff_seconds=2)

        first = manager.enqueue_and_wait(
            op_type="send_message",
            payload={"text": "I am currently"},
            chat_id=123,
            dedupe_key="group:chunk:0",
            allow_paused_return=True,
        )
        second = manager.enqueue_and_wait(
            op_type="send_message",
            payload={"text": "I am currently running a deeper scan"},
            chat_id=123,
            dedupe_key="group:chunk:0",
            allow_paused_return=True,
        )

        self.assertEqual(first["status"], "queued")
        self.assertEqual(second["status"], "queued")

        with closing(sqlite3.connect(self.paths.database)) as connection:
            rows = connection.execute(
                """
                SELECT queue_id, payload_json
                FROM telegram_outbound_queue
                WHERE dedupe_key = ?
                ORDER BY created_at
                """,
                ("group:chunk:0",),
            ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(json.loads(str(rows[0][1]))["text"], "I am currently running a deeper scan")

        self._expire_pause_and_queue()
        result = manager.process_next()

        self.assertIsNotNone(result)
        self.assertEqual(telegram.messages, [(123, "I am currently running a deeper scan")])

    def test_rate_limit_backoff_pauses_all_queue_work_and_grows_exponentially(self) -> None:
        telegram = RateLimitedTelegram(edit_failures=2)
        manager = TelegramDeliveryManager(self.paths, telegram, run_id="run-1")
        ServiceRunStore(self.paths).start(run_id="run-1", pid=1)

        manager.enqueue(
            op_type="edit_message",
            payload={"message_id": 9, "text": "first"},
            chat_id=321,
            telegram_message_id=9,
            dedupe_key="group:chunk:0",
        )
        self._expire_pause_and_queue()
        first_result = manager.process_next()

        self.assertIsNotNone(first_result)
        self.assertEqual(first_result["status"], "queued")
        state = self._read_app_state(_RATE_LIMIT_STATE_KEY)
        self.assertEqual(state["backoff_seconds"], 2)

        with closing(sqlite3.connect(self.paths.database)) as connection:
            queued_row = connection.execute(
                "SELECT available_at FROM telegram_outbound_queue WHERE dedupe_key = ?",
                ("group:chunk:0",),
            ).fetchone()
        self.assertIsNotNone(queued_row)
        first_available_at = datetime.fromisoformat(str(queued_row[0]))
        self.assertGreater(first_available_at, datetime.now(timezone.utc))

        queued_typing = manager.enqueue_and_wait(
            op_type="typing",
            payload={},
            chat_id=321,
            allow_paused_return=True,
        )
        self.assertEqual(queued_typing["status"], "queued")
        with closing(sqlite3.connect(self.paths.database)) as connection:
            typing_row = connection.execute(
                "SELECT available_at FROM telegram_outbound_queue WHERE op_type = 'typing'"
            ).fetchone()
        self.assertEqual(str(typing_row[0]), str(queued_row[0]))

        self._expire_pause_and_queue(backoff_seconds=2)
        second_result = manager.process_next()

        self.assertIsNotNone(second_result)
        self.assertEqual(second_result["status"], "queued")
        state = self._read_app_state(_RATE_LIMIT_STATE_KEY)
        self.assertEqual(state["backoff_seconds"], 4)

    def test_bare_http_429_is_treated_as_rate_limit(self) -> None:
        telegram = BareHttp429Telegram()
        manager = TelegramDeliveryManager(self.paths, telegram, run_id="run-1")
        ServiceRunStore(self.paths).start(run_id="run-1", pid=1)

        manager.enqueue(
            op_type="edit_message",
            payload={"message_id": 9, "text": "first"},
            chat_id=321,
            telegram_message_id=9,
            dedupe_key="group:chunk:0",
        )
        self._expire_pause_and_queue()
        result = manager.process_next()

        self.assertIsNotNone(result)
        self.assertEqual(result["status"], "queued")
        state = self._read_app_state(_RATE_LIMIT_STATE_KEY)
        self.assertEqual(state["backoff_seconds"], 2)

    def test_first_rate_limited_send_returns_queued_without_preexisting_pause(self) -> None:
        telegram = SendRateLimitedTelegram(send_failures=1)
        manager = TelegramDeliveryManager(self.paths, telegram, run_id="run-1")
        ServiceRunStore(self.paths).start(run_id="run-1", pid=1)
        manager.start()
        self.addCleanup(manager.stop)

        result = manager.enqueue_and_wait(
            op_type="send_message",
            payload={"text": "hello"},
            chat_id=321,
        )

        self.assertEqual(result["status"], "queued")
        state = self._read_app_state(_RATE_LIMIT_STATE_KEY)
        self.assertEqual(state["backoff_seconds"], 2)

    def test_unpaused_queue_uses_throttle_window_to_keep_only_latest_chunk(self) -> None:
        telegram = FakeTelegramClient()
        manager = TelegramDeliveryManager(self.paths, telegram, run_id="run-1")
        ServiceRunStore(self.paths).start(run_id="run-1", pid=1)

        manager.enqueue(
            op_type="send_message",
            payload={"text": "I am cur"},
            chat_id=123,
            dedupe_key="group:chunk:0",
        )
        manager.enqueue(
            op_type="send_message",
            payload={"text": "I am currently running"},
            chat_id=123,
            dedupe_key="group:chunk:0",
        )

        with closing(sqlite3.connect(self.paths.database)) as connection:
            row = connection.execute(
                """
                SELECT payload_json, available_at
                FROM telegram_outbound_queue
                WHERE dedupe_key = ?
                """,
                ("group:chunk:0",),
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(json.loads(str(row[0]))["text"], "I am currently running")
        available_at = datetime.fromisoformat(str(row[1]))
        self.assertGreater(available_at, datetime.now(timezone.utc))

        self._expire_pause_and_queue()
        result = manager.process_next()

        self.assertIsNotNone(result)
        self.assertEqual(telegram.messages, [(123, "I am currently running")])

    def test_in_flight_send_replacement_is_promoted_to_edit(self) -> None:
        manager: TelegramDeliveryManager | None = None

        def enqueue_replacement() -> None:
            assert manager is not None
            manager.enqueue(
                op_type="send_message",
                payload={"text": "I’m tightening the queue lifecycle"},
                chat_id=123,
                dedupe_key="group:chunk:0",
                message_group_id="group",
            )

        telegram = CallbackTelegram(on_send=enqueue_replacement)
        manager = TelegramDeliveryManager(self.paths, telegram, run_id="run-1")
        ServiceRunStore(self.paths).start(run_id="run-1", pid=1)

        manager.enqueue(
            op_type="send_message",
            payload={"text": "I’m tightening"},
            chat_id=123,
            dedupe_key="group:chunk:0",
            message_group_id="group",
        )

        self._expire_pause_and_queue()
        first = manager.process_next()
        time.sleep(0.06)
        self._expire_pause_and_queue()
        second = manager.process_next()

        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertEqual(telegram.messages, [(123, "I’m tightening")])
        self.assertEqual(telegram.edits, [(123, 1, "I’m tightening the queue lifecycle")])

        with closing(sqlite3.connect(self.paths.database)) as connection:
            rows = connection.execute(
                """
                SELECT op_type, telegram_message_id
                FROM telegram_outbound_queue
                WHERE dedupe_key = ?
                ORDER BY created_at ASC
                """,
                ("group:chunk:0",),
            ).fetchall()
        self.assertEqual([str(row[0]) for row in rows], ["send_message", "edit_message"])
        self.assertEqual([int(row[1]) for row in rows], [1, 1])

    def test_stale_live_progress_row_is_skipped_after_final_delivery_starts(self) -> None:
        telegram = FakeTelegramClient()
        manager = TelegramDeliveryManager(self.paths, telegram, run_id="run-1")
        ServiceRunStore(self.paths).start(run_id="run-1", pid=1)
        auth = AuthState(bot_token="token", telegram_user_id=11, telegram_chat_id=123, paired_at="now")
        store = SessionStore(self.paths)
        session = store.get_or_create_telegram_session(auth)
        session.active_turn_id = "turn-1"
        session.current_trace_id = "trace-1"
        session.status = "RUNNING_TURN"
        session.last_user_message_at = datetime.now(timezone.utc).isoformat()
        store.save_session(session)
        stale_group = f"{session.session_id}:live_progress:trace-1:{session.last_user_message_at}"

        manager.enqueue(
            op_type="send_message",
            payload={"text": "Thinking..."},
            chat_id=123,
            session_id=session.session_id,
            message_group_id=stale_group,
            dedupe_key=f"{stale_group}:chunk:0",
        )

        session.status = "DELIVERING_FINAL"
        session.active_turn_id = None
        store.save_session(session)
        self._expire_pause_and_queue()
        result = manager.process_next()

        self.assertIsNotNone(result)
        self.assertTrue(result["skipped"])
        self.assertEqual(result["skip_reason"], "session_status:DELIVERING_FINAL")
        self.assertEqual(telegram.messages, [])
        with closing(sqlite3.connect(self.paths.database)) as connection:
            status_row = connection.execute(
                "SELECT status FROM telegram_outbound_queue WHERE queue_id = ?",
                (result["queue_id"],),
            ).fetchone()
            event_row = connection.execute(
                """
                SELECT payload_json
                FROM events
                WHERE event_type = 'telegram.queue.skipped_stale'
                ORDER BY event_id DESC
                LIMIT 1
                """
            ).fetchone()
        self.assertEqual(str(status_row[0]), "completed")
        self.assertIsNotNone(event_row)
        self.assertEqual(json.loads(str(event_row[0]))["reason"], "session_status:DELIVERING_FINAL")

    def test_stale_typing_row_is_skipped_after_turn_finishes(self) -> None:
        telegram = FakeTelegramClient()
        manager = TelegramDeliveryManager(self.paths, telegram, run_id="run-1")
        ServiceRunStore(self.paths).start(run_id="run-1", pid=1)
        auth = AuthState(bot_token="token", telegram_user_id=11, telegram_chat_id=123, paired_at="now")
        store = SessionStore(self.paths)
        session = store.get_or_create_telegram_session(auth)
        session.active_turn_id = "turn-1"
        session.current_trace_id = "trace-1"
        session.status = "RUNNING_TURN"
        session.last_user_message_at = datetime.now(timezone.utc).isoformat()
        store.save_session(session)

        manager.enqueue(
            op_type="typing",
            payload={},
            chat_id=123,
            session_id=session.session_id,
            message_group_id=f"{session.session_id}:typing",
            dedupe_key=f"{session.session_id}:typing",
        )

        session.status = "ACTIVE"
        session.active_turn_id = None
        store.save_session(session)
        self._expire_pause_and_queue()
        result = manager.process_next()

        self.assertIsNotNone(result)
        self.assertTrue(result["skipped"])
        self.assertEqual(result["skip_reason"], "session_status:ACTIVE")
        self.assertEqual(telegram.typing_actions, [])


if __name__ == "__main__":
    unittest.main()
