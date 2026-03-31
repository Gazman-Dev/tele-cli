from __future__ import annotations

import json
import sqlite3
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core.paths import build_paths
from integrations.telegram import TelegramError
from runtime import service as service_module  # Preload runtime/storage graph before importing the queue module.
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


class TelegramQueueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.paths = build_paths(Path.cwd() / ".test_state" / "telegram_queue" / str(uuid.uuid4()))

    def _read_app_state(self, key: str) -> dict:
        with sqlite3.connect(self.paths.database) as connection:
            row = connection.execute("SELECT value_json FROM app_state WHERE state_key = ?", (key,)).fetchone()
        if row is None:
            return {}
        return json.loads(str(row[0]))

    def _set_pause_state(self, *, paused_until: datetime, backoff_seconds: int) -> None:
        with sqlite3.connect(self.paths.database) as connection:
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

    def _expire_pause_and_queue(self, *, backoff_seconds: int = 0) -> None:
        expired = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        with sqlite3.connect(self.paths.database) as connection:
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

        with sqlite3.connect(self.paths.database) as connection:
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

        with sqlite3.connect(self.paths.database) as connection:
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
        with sqlite3.connect(self.paths.database) as connection:
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

        with sqlite3.connect(self.paths.database) as connection:
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


if __name__ == "__main__":
    unittest.main()
