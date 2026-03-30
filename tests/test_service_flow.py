from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
import sqlite3
import uuid
import unittest
from pathlib import Path
from unittest.mock import patch

from core.json_store import load_json
from core.models import AuthState, CodexServerState, Config, RuntimeState
from core.paths import build_paths
from integrations.telegram import TelegramError
from runtime.approval_store import ApprovalRecord, ApprovalStore
from runtime.app_server_runtime import make_app_server_start_fn
from runtime.performance import PerformanceTracker
from runtime.runtime import ServiceRuntime
from runtime.service import (
    bootstrap_paired_codex,
    drain_codex_approvals,
    drain_codex_notifications,
    ensure_thinking_message,
    extract_activity_text,
    extract_assistant_text,
    extract_event_driven_status,
    flush_buffer,
    flush_idle_partial_outputs,
    is_default_thinking_text,
    maybe_refresh_thinking_message,
    maybe_send_typing_indicator,
    process_telegram_update,
    extract_login_callback_url,
    replay_login_callback,
)
from runtime.session_store import SessionStore
from runtime.telegram_html import escape_telegram_html, render_final_telegram_html, render_telegram_progress_html, to_telegram_html
from runtime.telegram_markdown import normalize_telegram_markdown_source
from storage.runtime_state_store import load_codex_server_state, save_codex_server_state
from tests.fakes.fake_app_server import FakeAppServer, InMemoryJsonRpcTransport
from tests.fakes.fake_telegram import FakeTelegramClient


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
        if op_type == "send_photo":
            return self._call(
                target.send_photo,
                chat_id,
                payload["photo_path"],
                topic_id=topic_id,
                caption=payload.get("caption"),
                parse_mode=payload.get("parse_mode"),
            )
        if op_type == "send_document":
            return self._call(
                target.send_document,
                chat_id,
                payload["document_path"],
                topic_id=topic_id,
                caption=payload.get("caption"),
                parse_mode=payload.get("parse_mode"),
            )
        raise RuntimeError(f"Unsupported op_type {op_type!r} in test delivery manager.")


def load_event_types(paths) -> list[str]:
    with sqlite3.connect(paths.database) as connection:
        rows = connection.execute("SELECT event_type FROM events ORDER BY event_id").fetchall()
    return [str(row[0]) for row in rows]


def load_events(paths, *, event_type: str | None = None) -> list[dict]:
    query = """
        SELECT event_type, source, trace_id, session_id, thread_id, turn_id, source_event_id, chat_id, topic_id, payload_json
        FROM events
    """
    params: tuple[object, ...] = ()
    if event_type is not None:
        query += " WHERE event_type = ?"
        params = (event_type,)
    query += " ORDER BY event_id"
    with sqlite3.connect(paths.database) as connection:
        rows = connection.execute(query, params).fetchall()
    events: list[dict] = []
    for row in rows:
        payload = json.loads(str(row[9])) if row[9] else None
        events.append(
            {
                "event_type": str(row[0]),
                "source": str(row[1]),
                "trace_id": row[2],
                "session_id": row[3],
                "thread_id": row[4],
                "turn_id": row[5],
                "source_event_id": row[6],
                "chat_id": row[7],
                "topic_id": row[8],
                "payload": payload,
            }
        )
    return events


def load_recovery_messages(paths) -> list[str]:
    with sqlite3.connect(paths.database) as connection:
        rows = connection.execute(
            "SELECT payload_json FROM events WHERE source = 'service' AND event_type = 'service.recovery' ORDER BY event_id"
        ).fetchall()
    return [json.loads(str(row[0])).get("message", "") for row in rows]


class FakeRecorder:
    def __init__(self) -> None:
        self.records: list[tuple[str, str]] = []

    def record(self, source: str, line: str) -> None:
        self.records.append((source, line))


class FakeCodex:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.sent_topics: list[int | None] = []
        self.interrupted = False
        self.stopped = False
        self.stop_result = False
        self.interrupt_topics: list[int | None] = []
        self.approved: list[int] = []
        self.denied: list[int] = []
        self.pending_approvals: list[ApprovalRecord] = []
        self.pending_notifications: list[object] = []

    def send(self, text: str, topic_id: int | None = None) -> None:
        self.sent.append(text)
        self.sent_topics.append(topic_id)

    def interrupt(self, topic_id: int | None = None) -> bool:
        self.interrupted = True
        self.interrupt_topics.append(topic_id)
        return self.stop_result

    def stop(self) -> None:
        self.stopped = True

    def poll_approval_request(self):
        if self.pending_approvals:
            return self.pending_approvals.pop(0)
        return None

    def approve(self, request_id: int) -> None:
        self.approved.append(request_id)

    def deny(self, request_id: int) -> None:
        self.denied.append(request_id)

    def poll_notification(self):
        if self.pending_notifications:
            return self.pending_notifications.pop(0)
        return None


class RecoveringCodex(FakeCodex):
    def send(
        self,
        text: str,
        topic_id: int | None = None,
        chat_id: int | None = None,
        user_id: int | None = None,
    ) -> bool:
        self.sent.append(text)
        self.sent_topics.append(topic_id)
        return True


class ServiceFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.paths = build_paths(Path.cwd() / ".test_state" / "service_flow" / str(uuid.uuid4()))
        self.config = Config(state_dir=str(self.paths.root))
        self.runtime_state = RuntimeState(
            session_id="1",
            service_state="RUNNING",
            codex_state="STOPPED",
            telegram_state="RUNNING",
            recorder_state="RUNNING",
            debug_state="RUNNING",
        )
        self.runtime = ServiceRuntime(self.runtime_state)
        self.recorder = FakeRecorder()
        self.metadata = object()
        self.app_lock = object()
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

    def test_status_update_is_handled_without_starting_codex(self) -> None:
        auth = AuthState(
            bot_token="token",
            telegram_user_id=11,
            telegram_chat_id=22,
            paired_at="now",
        )
        self.runtime_state.codex_state = "DEGRADED"
        telegram = FakeTelegramClient()
        update = {"update_id": 1, "message": {"chat": {"id": 22}, "from": {"id": 11}, "text": "/status"}}

        def fail_start(*args, **kwargs):
            raise AssertionError("status handling should not attempt to start Codex")

        with patch("runtime.service.save_json"):
            codex = process_telegram_update(
                update,
                paths=self.paths,
                config=self.config,
                auth=auth,
                runtime=self.runtime,
                runtime_state=self.runtime_state,
                metadata=self.metadata,
                app_lock=self.app_lock,
                telegram=telegram,
                recorder=self.recorder,
                codex=None,
                handle_output=lambda source, line: None,
                start_codex_session_fn=fail_start,
            )

        self.assertIsNone(codex)

    def test_process_telegram_update_notifies_user_when_stale_turn_is_recovered(self) -> None:
        auth = AuthState(
            bot_token="token",
            telegram_user_id=11,
            telegram_chat_id=22,
            paired_at="now",
        )
        telegram = FakeTelegramClient()
        codex = RecoveringCodex()
        update = {"update_id": 1, "message": {"chat": {"id": 22}, "from": {"id": 11}, "text": "hello again"}}

        result = process_telegram_update(
            update,
            paths=self.paths,
            config=self.config,
            auth=auth,
            runtime=self.runtime,
            runtime_state=self.runtime_state,
            metadata=self.metadata,
            app_lock=self.app_lock,
            telegram=telegram,
            recorder=self.recorder,
            codex=codex,
            handle_output=lambda source, line: None,
        )

        self.assertIs(result, codex)
        self.assertEqual(codex.sent, ["hello again"])
        self.assertEqual(
            telegram.messages,
            [(22, "Something went wrong with the previous message. I recovered the session and restarted your request.")],
        )

    def test_status_shows_recovering_turn_state(self) -> None:
        auth = AuthState(
            bot_token="token",
            telegram_user_id=11,
            telegram_chat_id=22,
            paired_at="now",
        )
        store = SessionStore(self.paths)
        session = store.get_or_create_telegram_session(auth)
        session.thread_id = "thread-1"
        session.active_turn_id = "turn-1"
        session.status = "RECOVERING_TURN"
        store.save_session(session)
        telegram = FakeTelegramClient()
        update = {"update_id": 2, "message": {"chat": {"id": 22}, "from": {"id": 11}, "text": "/status"}}

        with patch("runtime.service.save_json"):
            process_telegram_update(
                update,
                paths=self.paths,
                config=self.config,
                auth=auth,
                runtime=self.runtime,
                runtime_state=self.runtime_state,
                metadata=self.metadata,
                app_lock=self.app_lock,
                telegram=telegram,
                recorder=self.recorder,
                codex=None,
                handle_output=lambda source, line: None,
            )

        self.assertEqual(len(telegram.messages), 1)

    def test_process_telegram_update_clears_stale_pending_output_before_new_turn(self) -> None:
        auth = AuthState(
            bot_token="token",
            telegram_user_id=11,
            telegram_chat_id=22,
            paired_at="now",
        )
        store = SessionStore(self.paths)
        session = store.get_or_create_telegram_session(auth)
        session.thread_id = "thread-1"
        session.pending_output_text = "Old answer"
        session.streaming_output_text = "Old answer"
        session.thinking_history_text = "Old thinking"
        session.streaming_message_id = 7
        session.streaming_message_ids = [7]
        store.save_session(session)
        telegram = FakeTelegramClient()
        codex = FakeCodex()
        update = {"update_id": 10, "message": {"chat": {"id": 22}, "from": {"id": 11}, "text": "new request"}}

        process_telegram_update(
            update,
            paths=self.paths,
            config=self.config,
            auth=auth,
            runtime=self.runtime,
            runtime_state=self.runtime_state,
            metadata=self.metadata,
            app_lock=self.app_lock,
            telegram=telegram,
            recorder=self.recorder,
            codex=codex,
            handle_output=lambda source, line: None,
        )

        updated = store.get_or_create_telegram_session(auth)
        self.assertEqual(updated.pending_output_text, "")
        self.assertEqual(updated.streaming_output_text, "")
        self.assertEqual(updated.thinking_history_text, "")
        self.assertEqual(updated.streaming_message_ids, [])
        self.assertIsNone(updated.streaming_message_id)
        self.assertEqual(telegram.deletes, [(22, 7)])

    def test_turn_completed_without_new_output_does_not_reuse_previous_answer(self) -> None:
        auth = AuthState(
            bot_token="token",
            telegram_user_id=11,
            telegram_chat_id=22,
            paired_at="now",
        )
        store = SessionStore(self.paths)
        session = store.get_or_create_telegram_session(auth)
        session.thread_id = "thread-1"
        session.active_turn_id = "turn-2"
        session.status = "RUNNING_TURN"
        session.last_delivered_output_text = "Previous answer"
        session.streaming_message_id = 9
        session.streaming_message_ids = [9]
        store.save_session(session)
        telegram = FakeTelegramClient()

        class ReadableCodex(FakeCodex):
            def read_thread(self, thread_id: str, include_turns: bool = True):
                return {
                    "thread": {
                        "turns": [
                            {
                                "items": [
                                    {"type": "agentMessage", "text": "Previous answer"},
                                ]
                            }
                        ]
                    }
                }

        class Notification:
            def __init__(self, method: str, params: dict):
                self.method = method
                self.params = params

        codex = ReadableCodex()
        codex.pending_notifications.append(Notification("turn/completed", {"threadId": "thread-1", "turn": {"id": "turn-2"}}))

        drain_codex_notifications(self.paths, auth, telegram, self.recorder, codex)

        updated = store.get_or_create_telegram_session(auth)
        self.assertEqual(telegram.messages, [])
        self.assertEqual(telegram.edits, [])
        self.assertEqual(telegram.deletes, [(22, 9)])
        self.assertEqual(updated.last_delivered_output_text, "Previous answer")
        self.assertEqual(updated.pending_output_text, "")
        self.assertIsNone(updated.streaming_message_id)

    def test_request_failure_appends_error_below_live_message(self) -> None:
        auth = AuthState(
            bot_token="token",
            telegram_user_id=11,
            telegram_chat_id=22,
            paired_at="now",
        )
        store = SessionStore(self.paths)
        session = store.get_or_create_telegram_session(auth)
        session.thread_id = "thread-1"
        session.active_turn_id = "turn-1"
        session.status = "RUNNING_TURN"
        session.streaming_message_id = 5
        session.streaming_message_ids = [5]
        session.thinking_history_order = ["commentary:msg-1"]
        session.thinking_history_by_source = {"commentary:msg-1": "Checking logs"}
        session.thinking_live_texts = {"commentary:msg-1": "Checking logs"}
        store.save_session(session)
        telegram = FakeTelegramClient()

        class BrokenCodex(FakeCodex):
            def send(self, text: str, topic_id: int | None = None) -> None:
                raise RuntimeError("{'code': -32600, 'message': 'Invalid request: missing field `threadId`'}")

        update = {"update_id": 11, "message": {"chat": {"id": 22}, "from": {"id": 11}, "text": "is it still running?"}}

        process_telegram_update(
            update,
            paths=self.paths,
            config=self.config,
            auth=auth,
            runtime=self.runtime,
            runtime_state=self.runtime_state,
            metadata=self.metadata,
            app_lock=self.app_lock,
            telegram=telegram,
            recorder=self.recorder,
            codex=BrokenCodex(),
            handle_output=lambda source, line: None,
        )

        self.assertEqual(len(telegram.edits), 1)
        edited = telegram.edits[0][2]
        self.assertIn("Checking logs", edited)
        self.assertIn("<pre><code>", edited)
        self.assertIn("Invalid request: missing field `threadId`", edited)
        updated = store.get_or_create_telegram_session(auth)
        self.assertIsNone(updated.streaming_message_id)
        self.assertEqual(updated.streaming_message_ids, [])

    def test_regular_update_starts_codex_and_forwards_message(self) -> None:
        auth = AuthState(
            bot_token="token",
            telegram_user_id=11,
            telegram_chat_id=22,
            paired_at="now",
        )
        telegram = FakeTelegramClient()
        update = {"update_id": 1, "message": {"chat": {"id": 22}, "from": {"id": 11}, "text": "hello"}}
        started_codex = FakeCodex()

        with patch("runtime.service.save_json"):
            codex = process_telegram_update(
                update,
                paths=self.paths,
                config=self.config,
                auth=auth,
                runtime=self.runtime,
                runtime_state=self.runtime_state,
                metadata=self.metadata,
                app_lock=self.app_lock,
                telegram=telegram,
                recorder=self.recorder,
                codex=None,
                handle_output=lambda source, line: None,
                start_codex_session_fn=lambda *args, **kwargs: started_codex,
            )

        self.assertIs(codex, started_codex)
        self.assertEqual(started_codex.sent, ["hello"])
        self.assertEqual(started_codex.sent_topics, [None])
        self.assertEqual(self.recorder.records, [("telegram", "hello")])
        self.assertEqual(telegram.messages, [])

    def test_regular_update_routes_message_to_topic_specific_session(self) -> None:
        auth = AuthState(
            bot_token="token",
            telegram_user_id=11,
            telegram_chat_id=22,
            paired_at="now",
        )
        telegram = FakeTelegramClient()
        update = {
            "update_id": 10,
            "message": {
                "chat": {"id": 22},
                "from": {"id": 11},
                "message_thread_id": 99,
                "text": "hello topic",
            },
        }
        started_codex = FakeCodex()

        with patch("runtime.service.save_json"):
            codex = process_telegram_update(
                update,
                paths=self.paths,
                config=self.config,
                auth=auth,
                runtime=self.runtime,
                runtime_state=self.runtime_state,
                metadata=self.metadata,
                app_lock=self.app_lock,
                telegram=telegram,
                recorder=self.recorder,
                codex=None,
                handle_output=lambda source, line: None,
                start_codex_session_fn=lambda *args, **kwargs: started_codex,
            )

        self.assertIs(codex, started_codex)
        self.assertEqual(started_codex.sent, ["hello topic"])
        self.assertEqual(started_codex.sent_topics, [99])

    def test_regular_update_downloads_telegram_attachments_and_forwards_paths(self) -> None:
        auth = AuthState(
            bot_token="token",
            telegram_user_id=11,
            telegram_chat_id=22,
            paired_at="now",
        )
        telegram = FakeTelegramClient()
        telegram.files["doc-1"] = {"file_path": "documents/file_1.txt"}
        telegram.downloads["documents/file_1.txt"] = b"hello"
        telegram.files["photo-1"] = {"file_path": "photos/file_2.jpg"}
        telegram.downloads["photos/file_2.jpg"] = b"jpg"
        update = {
            "update_id": 12,
            "message": {
                "chat": {"id": 22},
                "from": {"id": 11},
                "caption": "please review",
                "document": {"file_id": "doc-1", "file_name": "notes.txt", "mime_type": "text/plain"},
                "photo": [{"file_id": "photo-1", "file_unique_id": "uniq-photo"}],
            },
        }
        started_codex = FakeCodex()

        with patch("runtime.service.save_json"):
            codex = process_telegram_update(
                update,
                paths=self.paths,
                config=self.config,
                auth=auth,
                runtime=self.runtime,
                runtime_state=self.runtime_state,
                metadata=self.metadata,
                app_lock=self.app_lock,
                telegram=telegram,
                recorder=self.recorder,
                codex=None,
                handle_output=lambda source, line: None,
                start_codex_session_fn=lambda *args, **kwargs: started_codex,
            )

        self.assertIs(codex, started_codex)
        self.assertEqual(len(started_codex.sent), 1)
        self.assertIn("please review", started_codex.sent[0])
        self.assertIn("Telegram attachments:", started_codex.sent[0])
        self.assertIn("telegram_media/", started_codex.sent[0])
        self.assertEqual(len(list((self.paths.root / "telegram_media").glob("*"))), 2)

    def test_regular_update_allows_paired_user_in_different_chat(self) -> None:
        auth = AuthState(
            bot_token="token",
            telegram_user_id=11,
            telegram_chat_id=22,
            paired_at="now",
        )
        telegram = FakeTelegramClient()
        update = {
            "update_id": 11,
            "message": {
                "chat": {"id": 44},
                "from": {"id": 11},
                "message_thread_id": 77,
                "text": "hello group",
            },
        }
        started_codex = FakeCodex()

        with patch("runtime.service.save_json"):
            codex = process_telegram_update(
                update,
                paths=self.paths,
                config=self.config,
                auth=auth,
                runtime=self.runtime,
                runtime_state=self.runtime_state,
                metadata=self.metadata,
                app_lock=self.app_lock,
                telegram=telegram,
                recorder=self.recorder,
                codex=None,
                handle_output=lambda source, line: None,
                start_codex_session_fn=lambda *args, **kwargs: started_codex,
            )

        self.assertIs(codex, started_codex)
        self.assertEqual(started_codex.sent, ["hello group"])
        self.assertEqual(started_codex.sent_topics, [77])
        self.assertEqual(telegram.messages, [])

    def test_regular_update_resets_topic_session_after_thread_id_request_failure(self) -> None:
        auth = AuthState(
            bot_token="token",
            telegram_user_id=11,
            telegram_chat_id=22,
            paired_at="now",
        )
        store = SessionStore(self.paths)
        session = store.get_or_create_telegram_session(auth, topic_id=77)
        session.thread_id = "thread-stale"
        session.active_turn_id = "turn-stale"
        session.streaming_message_id = 9
        session.streaming_output_text = "partial"
        session.thinking_message_text = "Thinking"
        session.status = "RUNNING_TURN"
        store.save_session(session)

        class BrokenCodex:
            def send(self, text: str, topic_id: int | None = None, chat_id: int | None = None, user_id: int | None = None):
                raise RuntimeError("{'code': -32600, 'message': 'Invalid request: missing field `threadId`'}")

        telegram = FakeTelegramClient()
        update = {
            "update_id": 14,
            "message": {"chat": {"id": 22}, "from": {"id": 11}, "message_thread_id": 77, "text": "retry"},
        }

        with patch("runtime.service.save_json"):
            returned = process_telegram_update(
                update,
                paths=self.paths,
                config=self.config,
                auth=auth,
                runtime=self.runtime,
                runtime_state=self.runtime_state,
                metadata=self.metadata,
                app_lock=self.app_lock,
                telegram=telegram,
                recorder=self.recorder,
                codex=BrokenCodex(),
                handle_output=lambda source, line: None,
            )

        self.assertIsInstance(returned, BrokenCodex)
        refreshed = store.get_current_telegram_session(auth, 77)
        self.assertIsNotNone(refreshed)
        assert refreshed is not None
        self.assertIsNone(refreshed.thread_id)
        self.assertIsNone(refreshed.active_turn_id)
        self.assertEqual(refreshed.streaming_output_text, "")
        self.assertEqual(refreshed.thinking_message_text, "")
        self.assertEqual(refreshed.status, "ACTIVE")
        self.assertEqual(
            telegram.messages,
            [(22, "Codex request failed: {'code': -32600, 'message': 'Invalid request: missing field `threadId`'}", 77)],
        )

    def test_regular_update_is_blocked_while_session_is_recovering(self) -> None:
        auth = AuthState(
            bot_token="token",
            telegram_user_id=11,
            telegram_chat_id=22,
            paired_at="now",
        )
        store = SessionStore(self.paths)
        session = store.get_or_create_telegram_session(auth)
        session.thread_id = "thread-1"
        session.active_turn_id = "turn-1"
        session.status = "RECOVERING_TURN"
        store.save_session(session)
        telegram = FakeTelegramClient()
        update = {"update_id": 3, "message": {"chat": {"id": 22}, "from": {"id": 11}, "text": "hello"}}
        codex = FakeCodex()

        with patch("runtime.service.save_json"):
            returned = process_telegram_update(
                update,
                paths=self.paths,
                config=self.config,
                auth=auth,
                runtime=self.runtime,
                runtime_state=self.runtime_state,
                metadata=self.metadata,
                app_lock=self.app_lock,
                telegram=telegram,
                recorder=self.recorder,
                codex=codex,
                handle_output=lambda source, line: None,
            )

        self.assertIs(returned, codex)
        self.assertEqual(codex.sent, [])
        self.assertEqual(self.recorder.records, [])
        self.assertEqual(
            telegram.messages,
            [(22, "Current session is recovering an in-flight turn. Wait for recovery, use /stop, or start fresh with /new.")],
        )

    def test_sleep_command_runs_sleep_and_reports_completion(self) -> None:
        auth = AuthState(
            bot_token="token",
            telegram_user_id=11,
            telegram_chat_id=22,
            paired_at="now",
        )
        telegram = FakeTelegramClient()
        update = {"update_id": 30, "message": {"chat": {"id": 22}, "from": {"id": 11}, "text": "/sleep"}}

        with (
            patch("runtime.service.save_json"),
            patch("runtime.service.run_sleep") as run_sleep_mock,
        ):
            returned = process_telegram_update(
                update,
                paths=self.paths,
                config=self.config,
                auth=auth,
                runtime=self.runtime,
                runtime_state=self.runtime_state,
                metadata=self.metadata,
                app_lock=self.app_lock,
                telegram=telegram,
                recorder=self.recorder,
                codex=None,
                handle_output=lambda source, line: None,
            )

        self.assertIsNone(returned)
        run_sleep_mock.assert_called_once()
        self.assertEqual(telegram.messages, [(22, "Sleep completed.")])

    def test_duplicate_update_id_does_not_forward_message_twice(self) -> None:
        auth = AuthState(
            bot_token="token",
            telegram_user_id=11,
            telegram_chat_id=22,
            paired_at="now",
        )
        telegram = FakeTelegramClient()
        update = {"update_id": 7, "message": {"chat": {"id": 22}, "from": {"id": 11}, "text": "hello"}}
        codex = FakeCodex()

        with patch("runtime.service.save_json"):
            returned = process_telegram_update(
                update,
                paths=self.paths,
                config=self.config,
                auth=auth,
                runtime=self.runtime,
                runtime_state=self.runtime_state,
                metadata=self.metadata,
                app_lock=self.app_lock,
                telegram=telegram,
                recorder=self.recorder,
                codex=codex,
                handle_output=lambda source, line: None,
            )
            returned = process_telegram_update(
                update,
                paths=self.paths,
                config=self.config,
                auth=auth,
                runtime=self.runtime,
                runtime_state=self.runtime_state,
                metadata=self.metadata,
                app_lock=self.app_lock,
                telegram=telegram,
                recorder=self.recorder,
                codex=returned,
                handle_output=lambda source, line: None,
            )

        self.assertIs(returned, codex)
        self.assertEqual(codex.sent, ["hello"])
        self.assertEqual(self.recorder.records, [("telegram", "hello")])
        self.assertEqual(telegram.messages, [])

    def test_duplicate_update_id_is_ignored_after_restart_style_reentry(self) -> None:
        auth = AuthState(
            bot_token="token",
            telegram_user_id=11,
            telegram_chat_id=22,
            paired_at="now",
        )
        telegram = FakeTelegramClient()
        update = {"update_id": 8, "message": {"chat": {"id": 22}, "from": {"id": 11}, "text": "hello"}}
        first_codex = FakeCodex()
        second_codex = FakeCodex()

        with patch("runtime.service.save_json"):
            returned = process_telegram_update(
                update,
                paths=self.paths,
                config=self.config,
                auth=auth,
                runtime=self.runtime,
                runtime_state=self.runtime_state,
                metadata=self.metadata,
                app_lock=self.app_lock,
                telegram=telegram,
                recorder=self.recorder,
                codex=first_codex,
                handle_output=lambda source, line: None,
            )
            returned = process_telegram_update(
                update,
                paths=self.paths,
                config=self.config,
                auth=auth,
                runtime=self.runtime,
                runtime_state=self.runtime_state,
                metadata=self.metadata,
                app_lock=self.app_lock,
                telegram=telegram,
                recorder=self.recorder,
                codex=second_codex,
                handle_output=lambda source, line: None,
            )

        self.assertIs(returned, second_codex)
        self.assertEqual(first_codex.sent, ["hello"])
        self.assertEqual(second_codex.sent, [])
        self.assertEqual(self.recorder.records, [("telegram", "hello")])
        self.assertEqual(telegram.messages, [])

    def test_duplicate_status_update_does_not_send_status_twice(self) -> None:
        auth = AuthState(
            bot_token="token",
            telegram_user_id=11,
            telegram_chat_id=22,
            paired_at="now",
        )
        self.runtime_state.codex_state = "DEGRADED"
        telegram = FakeTelegramClient()
        update = {"update_id": 9, "message": {"chat": {"id": 22}, "from": {"id": 11}, "text": "/status"}}

        with patch("runtime.service.save_json"):
            process_telegram_update(
                update,
                paths=self.paths,
                config=self.config,
                auth=auth,
                runtime=self.runtime,
                runtime_state=self.runtime_state,
                metadata=self.metadata,
                app_lock=self.app_lock,
                telegram=telegram,
                recorder=self.recorder,
                codex=None,
                handle_output=lambda source, line: None,
            )
            process_telegram_update(
                update,
                paths=self.paths,
                config=self.config,
                auth=auth,
                runtime=self.runtime,
                runtime_state=self.runtime_state,
                metadata=self.metadata,
                app_lock=self.app_lock,
                telegram=telegram,
                recorder=self.recorder,
                codex=None,
                handle_output=lambda source, line: None,
            )

        self.assertEqual(len(telegram.messages), 1)
        self.assertIn("codex=DEGRADED", telegram.messages[0][1])

    def test_first_message_issues_pairing_code_when_not_paired(self) -> None:
        auth = AuthState(bot_token="token")
        telegram = FakeTelegramClient()
        update = {"update_id": 1, "message": {"chat": {"id": 22}, "from": {"id": 11}, "text": "hello"}}

        with (
            patch("runtime.service.save_json"),
            patch("runtime.service.isatty", return_value=False),
        ):
            codex = process_telegram_update(
                update,
                paths=self.paths,
                config=self.config,
                auth=auth,
                runtime=self.runtime,
                runtime_state=self.runtime_state,
                metadata=self.metadata,
                app_lock=self.app_lock,
                telegram=telegram,
                recorder=self.recorder,
                codex=None,
                handle_output=lambda source, line: None,
            )

        self.assertIsNone(codex)
        self.assertEqual(auth.pending_chat_id, 22)
        self.assertEqual(auth.pending_user_id, 11)
        self.assertEqual(len(telegram.messages), 1)
        self.assertIn("Pairing code:", telegram.messages[0][1])
        pairing_event = next(event for event in load_events(self.paths) if event["event_type"] == "telegram.pairing.requested")
        self.assertEqual(pairing_event["source_event_id"], "1")
        self.assertEqual(pairing_event["chat_id"], 22)
        self.assertEqual(pairing_event["payload"]["pending_user_id"], 11)

    def test_process_telegram_update_logs_attachment_and_trace_linkage(self) -> None:
        auth = AuthState(
            bot_token="token",
            telegram_user_id=11,
            telegram_chat_id=22,
            paired_at="now",
        )
        telegram = FakeTelegramClient()
        telegram.files["doc-1"] = {"file_path": "documents/report.txt"}
        telegram.downloads["documents/report.txt"] = b"report-body"
        update = {
            "update_id": 7,
            "message": {
                "chat": {"id": 22},
                "from": {"id": 11},
                "caption": "please review",
                "document": {"file_id": "doc-1", "file_name": "report.txt", "mime_type": "text/plain"},
            },
        }
        codex = FakeCodex()

        with patch("runtime.service.save_json"):
            returned = process_telegram_update(
                update,
                paths=self.paths,
                config=self.config,
                auth=auth,
                runtime=self.runtime,
                runtime_state=self.runtime_state,
                metadata=self.metadata,
                app_lock=self.app_lock,
                telegram=telegram,
                recorder=self.recorder,
                codex=codex,
                handle_output=lambda source, line: None,
            )

        self.assertIs(returned, codex)
        self.assertEqual(len(codex.sent), 1)
        self.assertIn("Telegram attachments:", codex.sent[0])
        self.assertIn("File saved to", codex.sent[0])

        events = load_events(self.paths)
        trace_started = next(event for event in events if event["event_type"] == "trace.started")
        bound = next(event for event in events if event["event_type"] == "telegram.update.bound_to_trace")
        request_started = next(event for event in events if event["event_type"] == "ai.request.started")
        attachment_saved = next(event for event in events if event["event_type"] == "telegram.attachment.saved")

        self.assertEqual(trace_started["source_event_id"], "7")
        self.assertEqual(bound["source_event_id"], "7")
        self.assertEqual(trace_started["trace_id"], bound["trace_id"])
        self.assertEqual(bound["trace_id"], request_started["trace_id"])
        self.assertEqual(attachment_saved["source_event_id"], "7")
        self.assertEqual(attachment_saved["payload"]["file_id"], "doc-1")
        saved_relpath = attachment_saved["payload"]["saved_relpath"]
        self.assertTrue((self.paths.root / saved_relpath).exists())
        self.assertIn(saved_relpath, codex.sent[0])

    def test_bootstrap_paired_codex_uses_app_server_and_reports_running(self) -> None:
        auth = AuthState(
            bot_token="token",
            telegram_user_id=11,
            telegram_chat_id=22,
            paired_at="now",
        )
        telegram = FakeTelegramClient()
        transport = InMemoryJsonRpcTransport()
        server = FakeAppServer(transport)
        server.on("initialize", lambda payload: {"protocolVersion": "1.0", "capabilities": {"threads": True}})
        server.on("getAccount", lambda payload: {"status": "ready", "accountType": "chatgpt"})
        start_fn = make_app_server_start_fn(self.paths, lambda config, auth: transport)

        codex = bootstrap_paired_codex(
            paths=self.paths,
            config=self.config,
            auth=auth,
            runtime=self.runtime,
            runtime_state=self.runtime_state,
            metadata=self.metadata,
            app_lock=self.app_lock,
            telegram=telegram,
            handle_output=lambda source, line: None,
            codex=None,
            start_codex_session_fn=start_fn,
        )

        self.assertIsNotNone(codex)
        self.assertEqual(self.runtime_state.codex_state, "RUNNING")
        self.assertEqual(telegram.messages, [])

    def test_bootstrap_paired_codex_reports_auth_required_without_telegram_notice(self) -> None:
        auth = AuthState(
            bot_token="token",
            telegram_user_id=11,
            telegram_chat_id=22,
            paired_at="now",
        )
        telegram = FakeTelegramClient()
        transport = InMemoryJsonRpcTransport()
        server = FakeAppServer(transport)
        server.on("initialize", lambda payload: {"protocolVersion": "1.0", "capabilities": {"threads": True}})
        server.on("getAccount", lambda payload: {"status": "auth_required"})
        server.on("login/account", lambda payload: {"type": "chatgpt", "authUrl": "https://example.test/login"})
        start_fn = make_app_server_start_fn(self.paths, lambda config, auth: transport)

        codex = bootstrap_paired_codex(
            paths=self.paths,
            config=self.config,
            auth=auth,
            runtime=self.runtime,
            runtime_state=self.runtime_state,
            metadata=self.metadata,
            app_lock=self.app_lock,
            telegram=telegram,
            handle_output=lambda source, line: None,
            codex=None,
            start_codex_session_fn=start_fn,
        )

        self.assertIsNotNone(codex)
        self.assertEqual(self.runtime_state.codex_state, "AUTH_REQUIRED")
        self.assertEqual(telegram.messages, [])

    def test_extract_login_callback_url_requires_code_and_state(self) -> None:
        url = extract_login_callback_url(
            "done http://localhost:1455/auth/callback?code=abc123&state=xyz987 and more"
        )
        missing = extract_login_callback_url("http://localhost:1455/auth/callback?code=abc123")

        self.assertEqual(url, "http://localhost:1455/auth/callback?code=abc123&state=xyz987")
        self.assertIsNone(missing)

    def test_handle_authorized_message_replays_pasted_login_callback_when_auth_required(self) -> None:
        auth = AuthState(
            bot_token="token",
            telegram_user_id=11,
            telegram_chat_id=22,
            paired_at="now",
        )
        telegram = FakeTelegramClient()
        runtime_state = RuntimeState(
            session_id="1",
            service_state="RUNNING",
            codex_state="AUTH_REQUIRED",
            telegram_state="RUNNING",
            recorder_state="RUNNING",
            debug_state="RUNNING",
        )

        with patch("runtime.service.replay_login_callback", return_value=(True, "ok")) as replay:
            from runtime.service import handle_authorized_message

            handle_authorized_message(
                "http://localhost:1455/auth/callback?code=abc123&state=xyz987",
                auth,
                runtime_state,
                None,
                telegram,
                self.recorder,
                paths=self.paths,
                source_event_id="77",
            )

        replay.assert_called_once_with("http://localhost:1455/auth/callback?code=abc123&state=xyz987")
        self.assertEqual(
            telegram.messages,
            [(22, "Codex login callback received. Waiting for Codex to finish sign-in.")],
        )
        self.assertEqual(self.recorder.records, [])
        callback_events = load_events(self.paths)
        self.assertEqual(
            [event["event_type"] for event in callback_events if event["event_type"].startswith("codex.login_callback.")],
            ["codex.login_callback.received", "codex.login_callback.completed"],
        )
        self.assertTrue(all(event["source_event_id"] == "77" for event in callback_events if event["event_type"].startswith("codex.login_callback.")))

    def test_handle_authorized_message_reports_callback_replay_failure(self) -> None:
        auth = AuthState(
            bot_token="token",
            telegram_user_id=11,
            telegram_chat_id=22,
            paired_at="now",
        )
        telegram = FakeTelegramClient()
        runtime_state = RuntimeState(
            session_id="1",
            service_state="RUNNING",
            codex_state="AUTH_REQUIRED",
            telegram_state="RUNNING",
            recorder_state="RUNNING",
            debug_state="RUNNING",
        )

        with patch("runtime.service.replay_login_callback", return_value=(False, "Connection refused")):
            from runtime.service import handle_authorized_message

            handle_authorized_message(
                "http://localhost:1455/auth/callback?code=abc123&state=xyz987",
                auth,
                runtime_state,
                None,
                telegram,
                self.recorder,
                paths=self.paths,
                source_event_id="78",
            )

        self.assertEqual(
            telegram.messages,
            [(22, "Codex login callback failed: Connection refused")],
        )
        callback_events = load_events(self.paths)
        self.assertEqual(
            [event["event_type"] for event in callback_events if event["event_type"].startswith("codex.login_callback.")],
            ["codex.login_callback.received", "codex.login_callback.failed"],
        )
        self.assertTrue(all(event["source_event_id"] == "78" for event in callback_events if event["event_type"].startswith("codex.login_callback.")))

    def test_sessions_command_lists_current_chat_sessions(self) -> None:
        auth = AuthState(
            bot_token="token",
            telegram_user_id=11,
            telegram_chat_id=22,
            paired_at="now",
        )
        store = SessionStore(self.paths)
        session = store.get_or_create_telegram_session(auth)
        session.thread_id = "thread-1"
        store.save_session(session)
        telegram = FakeTelegramClient()
        update = {"update_id": 1, "message": {"chat": {"id": 22}, "from": {"id": 11}, "text": "/sessions"}}

        with patch("runtime.service.save_json"):
            codex = process_telegram_update(
                update,
                paths=self.paths,
                config=self.config,
                auth=auth,
                runtime=self.runtime,
                runtime_state=self.runtime_state,
                metadata=self.metadata,
                app_lock=self.app_lock,
                telegram=telegram,
                recorder=self.recorder,
                codex=None,
                handle_output=lambda source, line: None,
            )

        self.assertIsNone(codex)
        self.assertEqual(len(telegram.messages), 1)
        self.assertIn("Sessions", telegram.messages[0][1])
        self.assertIn("thread=thread-1", telegram.messages[0][1])

    def test_new_command_archives_previous_session_and_creates_new_one(self) -> None:
        auth = AuthState(
            bot_token="token",
            telegram_user_id=11,
            telegram_chat_id=22,
            paired_at="now",
        )
        store = SessionStore(self.paths)
        original = store.get_or_create_telegram_session(auth)
        telegram = FakeTelegramClient()
        update = {"update_id": 1, "message": {"chat": {"id": 22}, "from": {"id": 11}, "text": "/new"}}

        with patch("runtime.service.save_json"):
            codex = process_telegram_update(
                update,
                paths=self.paths,
                config=self.config,
                auth=auth,
                runtime=self.runtime,
                runtime_state=self.runtime_state,
                metadata=self.metadata,
                app_lock=self.app_lock,
                telegram=telegram,
                recorder=self.recorder,
                codex=None,
                handle_output=lambda source, line: None,
            )

        self.assertIsNone(codex)
        sessions = store.list_telegram_sessions(auth)
        self.assertEqual(len(sessions), 1)
        self.assertFalse(any(session.session_id == original.session_id for session in sessions))
        active = sessions[0]
        self.assertEqual(active.status, "ACTIVE")
        self.assertTrue(active.attached)
        self.assertEqual(telegram.messages, [(22, f"Started new session {active.session_id}.")])
        recovery_messages = load_recovery_messages(self.paths)
        self.assertTrue(any("session_detached_on_new" in message for message in recovery_messages))
        self.assertTrue(any("session_attached_on_new" in message for message in recovery_messages))

    def test_stop_command_interrupts_active_turn(self) -> None:
        auth = AuthState(
            bot_token="token",
            telegram_user_id=11,
            telegram_chat_id=22,
            paired_at="now",
        )
        telegram = FakeTelegramClient()
        codex = FakeCodex()
        codex.stop_result = True
        update = {"update_id": 1, "message": {"chat": {"id": 22}, "from": {"id": 11}, "text": "/stop"}}

        with patch("runtime.service.save_json"):
            returned = process_telegram_update(
                update,
                paths=self.paths,
                config=self.config,
                auth=auth,
                runtime=self.runtime,
                runtime_state=self.runtime_state,
                metadata=self.metadata,
                app_lock=self.app_lock,
                telegram=telegram,
                recorder=self.recorder,
                codex=codex,
                handle_output=lambda source, line: None,
            )

        self.assertIs(returned, codex)
        self.assertTrue(codex.interrupted)
        self.assertEqual(telegram.messages, [(22, "Stopped the active turn.")])

    def test_stop_command_passes_topic_id(self) -> None:
        auth = AuthState(
            bot_token="token",
            telegram_user_id=11,
            telegram_chat_id=22,
            paired_at="now",
        )
        telegram = FakeTelegramClient()
        codex = FakeCodex()
        codex.stop_result = True
        update = {
            "update_id": 11,
            "message": {"chat": {"id": 22}, "from": {"id": 11}, "message_thread_id": 99, "text": "/stop"},
        }

        with patch("runtime.service.save_json"):
            returned = process_telegram_update(
                update,
                paths=self.paths,
                config=self.config,
                auth=auth,
                runtime=self.runtime,
                runtime_state=self.runtime_state,
                metadata=self.metadata,
                app_lock=self.app_lock,
                telegram=telegram,
                recorder=self.recorder,
                codex=codex,
                handle_output=lambda source, line: None,
            )

        self.assertIs(returned, codex)
        self.assertEqual(codex.interrupt_topics, [99])

    def test_stop_command_is_noop_without_active_turn(self) -> None:
        auth = AuthState(
            bot_token="token",
            telegram_user_id=11,
            telegram_chat_id=22,
            paired_at="now",
        )
        telegram = FakeTelegramClient()
        codex = FakeCodex()
        codex.stop_result = False
        update = {"update_id": 1, "message": {"chat": {"id": 22}, "from": {"id": 11}, "text": "/stop"}}

        with patch("runtime.service.save_json"):
            returned = process_telegram_update(
                update,
                paths=self.paths,
                config=self.config,
                auth=auth,
                runtime=self.runtime,
                runtime_state=self.runtime_state,
                metadata=self.metadata,
                app_lock=self.app_lock,
                telegram=telegram,
                recorder=self.recorder,
                codex=codex,
                handle_output=lambda source, line: None,
            )

        self.assertIs(returned, codex)
        self.assertTrue(codex.interrupted)
        self.assertEqual(telegram.messages, [(22, "No active turn to stop.")])

    def test_abort_command_interrupts_active_turn(self) -> None:
        auth = AuthState(
            bot_token="token",
            telegram_user_id=11,
            telegram_chat_id=22,
            paired_at="now",
        )
        telegram = FakeTelegramClient()
        codex = FakeCodex()
        codex.stop_result = True
        update = {"update_id": 12, "message": {"chat": {"id": 22}, "from": {"id": 11}, "text": "/abort"}}

        with patch("runtime.service.save_json"):
            returned = process_telegram_update(
                update,
                paths=self.paths,
                config=self.config,
                auth=auth,
                runtime=self.runtime,
                runtime_state=self.runtime_state,
                metadata=self.metadata,
                app_lock=self.app_lock,
                telegram=telegram,
                recorder=self.recorder,
                codex=codex,
                handle_output=lambda source, line: None,
            )

        self.assertIs(returned, codex)
        self.assertTrue(codex.interrupted)
        self.assertEqual(telegram.messages, [(22, "Aborted the active turn.")])

    def test_model_command_updates_codex_config_and_restarts_runtime(self) -> None:
        auth = AuthState(
            bot_token="token",
            telegram_user_id=11,
            telegram_chat_id=22,
            paired_at="now",
        )
        telegram = FakeTelegramClient()
        codex = FakeCodex()
        restarted_codex = FakeCodex()
        update = {"update_id": 13, "message": {"chat": {"id": 22}, "from": {"id": 11}, "text": "/model gpt-5.4-mini"}}

        with (
            patch("runtime.service.save_json"),
            patch("runtime.service.write_codex_cli_preferences") as write_config,
            patch("runtime.service.restart_codex_runtime", return_value=restarted_codex) as restart_runtime,
        ):
            returned = process_telegram_update(
                update,
                paths=self.paths,
                config=self.config,
                auth=auth,
                runtime=self.runtime,
                runtime_state=self.runtime_state,
                metadata=self.metadata,
                app_lock=self.app_lock,
                telegram=telegram,
                recorder=self.recorder,
                codex=codex,
                handle_output=lambda source, line: None,
            )

        self.assertIs(returned, restarted_codex)
        write_config.assert_called_once_with(model="gpt-5.4-mini")
        restart_runtime.assert_called_once()
        self.assertEqual(telegram.messages, [(22, 'Model set to "gpt-5.4-mini". Codex runtime restarted.')])

    def test_reasoning_command_updates_codex_config_and_restarts_runtime(self) -> None:
        auth = AuthState(
            bot_token="token",
            telegram_user_id=11,
            telegram_chat_id=22,
            paired_at="now",
        )
        telegram = FakeTelegramClient()
        codex = FakeCodex()
        restarted_codex = FakeCodex()
        update = {"update_id": 14, "message": {"chat": {"id": 22}, "from": {"id": 11}, "text": "/reasoning low"}}

        with (
            patch("runtime.service.save_json"),
            patch("runtime.service.write_codex_cli_preferences") as write_config,
            patch("runtime.service.restart_codex_runtime", return_value=restarted_codex) as restart_runtime,
        ):
            returned = process_telegram_update(
                update,
                paths=self.paths,
                config=self.config,
                auth=auth,
                runtime=self.runtime,
                runtime_state=self.runtime_state,
                metadata=self.metadata,
                app_lock=self.app_lock,
                telegram=telegram,
                recorder=self.recorder,
                codex=codex,
                handle_output=lambda source, line: None,
            )

        self.assertIs(returned, restarted_codex)
        write_config.assert_called_once_with(reasoning="low")
        restart_runtime.assert_called_once()
        self.assertEqual(telegram.messages, [(22, 'Reasoning set to "low". Codex runtime restarted.')])

    def test_reasoning_command_rejects_unknown_values(self) -> None:
        auth = AuthState(
            bot_token="token",
            telegram_user_id=11,
            telegram_chat_id=22,
            paired_at="now",
        )
        telegram = FakeTelegramClient()
        codex = FakeCodex()
        update = {"update_id": 15, "message": {"chat": {"id": 22}, "from": {"id": 11}, "text": "/reasoning turbo"}}

        with (
            patch("runtime.service.save_json"),
            patch("runtime.service.write_codex_cli_preferences") as write_config,
            patch("runtime.service.restart_codex_runtime") as restart_runtime,
        ):
            returned = process_telegram_update(
                update,
                paths=self.paths,
                config=self.config,
                auth=auth,
                runtime=self.runtime,
                runtime_state=self.runtime_state,
                metadata=self.metadata,
                app_lock=self.app_lock,
                telegram=telegram,
                recorder=self.recorder,
                codex=codex,
                handle_output=lambda source, line: None,
            )

        self.assertIs(returned, codex)
        write_config.assert_not_called()
        restart_runtime.assert_not_called()
        self.assertEqual(telegram.messages, [(22, "Reasoning must be one of: minimal, low, medium, high, xhigh.")])

    def test_model_command_requires_value(self) -> None:
        auth = AuthState(
            bot_token="token",
            telegram_user_id=11,
            telegram_chat_id=22,
            paired_at="now",
        )
        telegram = FakeTelegramClient()
        codex = FakeCodex()
        update = {"update_id": 16, "message": {"chat": {"id": 22}, "from": {"id": 11}, "text": "/model"}}

        with patch("runtime.service.save_json"):
            returned = process_telegram_update(
                update,
                paths=self.paths,
                config=self.config,
                auth=auth,
                runtime=self.runtime,
                runtime_state=self.runtime_state,
                metadata=self.metadata,
                app_lock=self.app_lock,
                telegram=telegram,
                recorder=self.recorder,
                codex=codex,
                handle_output=lambda source, line: None,
            )

        self.assertIs(returned, codex)
        self.assertEqual(telegram.messages, [(22, "Usage: /model <name>")])

    def test_drain_codex_approvals_persists_and_notifies(self) -> None:
        auth = AuthState(
            bot_token="token",
            telegram_user_id=11,
            telegram_chat_id=22,
            paired_at="now",
        )
        telegram = FakeTelegramClient()
        codex = FakeCodex()
        codex.pending_approvals.append(ApprovalRecord(17, "approval/request", {"tool": "shell"}))

        drain_codex_approvals(self.paths, auth, telegram, codex)

        pending = ApprovalStore(self.paths).get_pending(17)
        self.assertIsNotNone(pending)
        self.assertEqual(
            telegram.messages,
            [(22, "Approval needed 17: approval/request. Reply with /approve 17 or /deny 17.")],
        )

    def test_approve_command_marks_pending_request(self) -> None:
        auth = AuthState(
            bot_token="token",
            telegram_user_id=11,
            telegram_chat_id=22,
            paired_at="now",
        )
        ApprovalStore(self.paths).add(ApprovalRecord(17, "approval/request", {"tool": "shell"}))
        telegram = FakeTelegramClient()
        codex = FakeCodex()
        update = {"update_id": 1, "message": {"chat": {"id": 22}, "from": {"id": 11}, "text": "/approve 17"}}

        with patch("runtime.service.save_json"):
            returned = process_telegram_update(
                update,
                paths=self.paths,
                config=self.config,
                auth=auth,
                runtime=self.runtime,
                runtime_state=self.runtime_state,
                metadata=self.metadata,
                app_lock=self.app_lock,
                telegram=telegram,
                recorder=self.recorder,
                codex=codex,
                handle_output=lambda source, line: None,
            )

        self.assertIs(returned, codex)
        self.assertEqual(codex.approved, [17])
        self.assertEqual(telegram.messages, [(22, "Approved request 17.")])
        self.assertIsNone(ApprovalStore(self.paths).get_pending(17))

    def test_deny_command_marks_pending_request(self) -> None:
        auth = AuthState(
            bot_token="token",
            telegram_user_id=11,
            telegram_chat_id=22,
            paired_at="now",
        )
        ApprovalStore(self.paths).add(ApprovalRecord(19, "approval/request", {"tool": "shell"}))
        telegram = FakeTelegramClient()
        codex = FakeCodex()
        update = {"update_id": 1, "message": {"chat": {"id": 22}, "from": {"id": 11}, "text": "/deny 19"}}

        with patch("runtime.service.save_json"):
            returned = process_telegram_update(
                update,
                paths=self.paths,
                config=self.config,
                auth=auth,
                runtime=self.runtime,
                runtime_state=self.runtime_state,
                metadata=self.metadata,
                app_lock=self.app_lock,
                telegram=telegram,
                recorder=self.recorder,
                codex=codex,
                handle_output=lambda source, line: None,
            )

        self.assertIs(returned, codex)
        self.assertEqual(codex.denied, [19])
        self.assertEqual(telegram.messages, [(22, "Denied request 19.")])
        self.assertIsNone(ApprovalStore(self.paths).get_pending(19))

    def test_drain_codex_notifications_clears_completed_turn(self) -> None:
        auth = AuthState(
            bot_token="token",
            telegram_user_id=11,
            telegram_chat_id=22,
            paired_at="now",
        )
        store = SessionStore(self.paths)
        session = store.get_or_create_telegram_session(auth)
        session.thread_id = "thread-1"
        session.active_turn_id = "turn-1"
        store.save_session(session)

        class Notification:
            def __init__(self, method: str, params: dict):
                self.method = method
                self.params = params

        codex = FakeCodex()
        codex.pending_notifications.append(Notification("turn/completed", {"turnId": "turn-1"}))

        drain_codex_notifications(self.paths, auth, FakeTelegramClient(), self.recorder, codex)

        updated = store.get_or_create_telegram_session(auth)
        self.assertIsNone(updated.active_turn_id)
        self.assertEqual(updated.status, "ACTIVE")

    def test_drain_codex_notifications_persists_thread_update(self) -> None:
        auth = AuthState(
            bot_token="token",
            telegram_user_id=11,
            telegram_chat_id=22,
            paired_at="now",
        )
        store = SessionStore(self.paths)
        session = store.get_or_create_telegram_session(auth)

        class Notification:
            def __init__(self, method: str, params: dict):
                self.method = method
                self.params = params

        codex = FakeCodex()
        codex.pending_notifications.append(Notification("thread/updated", {"threadId": "thread-9"}))

        drain_codex_notifications(self.paths, auth, FakeTelegramClient(), self.recorder, codex)

        updated = store.get_or_create_telegram_session(auth)
        self.assertEqual(updated.thread_id, "thread-9")

    def test_drain_codex_notifications_sends_final_answer_on_turn_completed(self) -> None:
        auth = AuthState(
            bot_token="token",
            telegram_user_id=11,
            telegram_chat_id=22,
            paired_at="now",
        )
        store = SessionStore(self.paths)
        session = store.get_or_create_telegram_session(auth)
        session.thread_id = "thread-1"
        session.active_turn_id = "turn-1"
        store.save_session(session)

        class Notification:
            def __init__(self, method: str, params: dict):
                self.method = method
                self.params = params

        telegram = FakeTelegramClient()
        codex = FakeCodex()
        codex.pending_notifications.append(
            Notification("turn/completed", {"turnId": "turn-1", "outputText": "Final answer from Codex"})
        )

        drain_codex_notifications(self.paths, auth, telegram, self.recorder, codex)

        updated = store.get_or_create_telegram_session(auth)
        self.assertIsNone(updated.active_turn_id)
        self.assertIsNotNone(updated.last_agent_message_at)
        self.assertEqual(telegram.messages, [(22, "Final answer from Codex")])
        self.assertEqual(self.recorder.records, [("assistant", "Final answer from Codex")])

    def test_performance_log_tracks_agent_and_telegram_timing(self) -> None:
        auth = AuthState(
            bot_token="token",
            telegram_user_id=11,
            telegram_chat_id=22,
            paired_at="now",
        )
        telegram = FakeTelegramClient()
        performance = PerformanceTracker(self.paths.performance_log)
        update = {"update_id": 50, "message": {"chat": {"id": 22}, "from": {"id": 11}, "text": "hello"}}
        codex = FakeCodex()

        with patch("runtime.service.save_json"):
            returned = process_telegram_update(
                update,
                paths=self.paths,
                config=self.config,
                auth=auth,
                runtime=self.runtime,
                runtime_state=self.runtime_state,
                metadata=self.metadata,
                app_lock=self.app_lock,
                telegram=telegram,
                recorder=self.recorder,
                codex=codex,
                handle_output=lambda source, line: None,
                performance=performance,
            )

        self.assertIs(returned, codex)
        store = SessionStore(self.paths)
        session = store.get_or_create_telegram_session(auth)
        session.thread_id = "thread-1"
        session.active_turn_id = "turn-1"
        session.status = "RUNNING_TURN"
        store.save_session(session)

        class Notification:
            def __init__(self, method: str, params: dict):
                self.method = method
                self.params = params

        codex.pending_notifications.append(Notification("assistant/message.delta", {"threadId": "thread-1", "text": "Hello "}))
        codex.pending_notifications.append(Notification("turn/completed", {"turnId": "turn-1", "outputText": "world"}))

        drain_codex_notifications(self.paths, auth, telegram, self.recorder, codex, performance=performance)

        records = [
            json.loads(line)
            for line in self.paths.performance_log.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        events = [record["event"] for record in records]

        self.assertIn("telegram_message_received", events)
        self.assertIn("agent_request_started", events)
        self.assertIn("agent_reply_started", events)
        self.assertIn("agent_reply_finished", events)
        self.assertIn("telegram_send_started", events)
        self.assertIn("telegram_send_completed", events)
        db_events = load_events(self.paths)
        trace_started = next(event for event in db_events if event["event_type"] == "trace.started")
        bound = next(event for event in db_events if event["event_type"] == "telegram.update.bound_to_trace")
        request_started = next(event for event in db_events if event["event_type"] == "ai.request.started")
        reply_started = next(event for event in db_events if event["event_type"] == "ai.reply.started")
        reply_finished = next(event for event in db_events if event["event_type"] == "ai.reply.finished")
        trace_completed = next(event for event in db_events if event["event_type"] == "trace.completed")
        self.assertEqual(trace_started["source_event_id"], "50")
        self.assertEqual(bound["source_event_id"], "50")
        self.assertEqual(trace_started["trace_id"], bound["trace_id"])
        self.assertEqual(bound["trace_id"], request_started["trace_id"])
        self.assertEqual(request_started["trace_id"], reply_started["trace_id"])
        self.assertEqual(reply_started["trace_id"], reply_finished["trace_id"])
        self.assertEqual(reply_finished["trace_id"], trace_completed["trace_id"])

    def test_drain_codex_notifications_reads_nested_turn_id_and_thread_fallback_text(self) -> None:
        auth = AuthState(
            bot_token="token",
            telegram_user_id=11,
            telegram_chat_id=22,
            paired_at="now",
        )
        store = SessionStore(self.paths)
        session = store.get_or_create_telegram_session(auth)
        session.thread_id = "thread-1"
        session.active_turn_id = "turn-1"
        store.save_session(session)

        class Notification:
            def __init__(self, method: str, params: dict):
                self.method = method
                self.params = params

        class ReadableCodex(FakeCodex):
            def read_thread(self, thread_id: str, include_turns: bool = True):
                return {
                    "thread": {
                        "id": thread_id,
                        "turns": [
                            {
                                "id": "turn-1",
                                "items": [
                                    {"id": "m-1", "type": "agentMessage", "text": "Reply from thread/read"}
                                ],
                            }
                        ],
                    }
                }

        telegram = FakeTelegramClient()
        codex = ReadableCodex()
        codex.pending_notifications.append(
            Notification("turn/completed", {"threadId": "thread-1", "turn": {"id": "turn-1", "items": []}})
        )

        drain_codex_notifications(self.paths, auth, telegram, self.recorder, codex)

        updated = store.get_or_create_telegram_session(auth)
        self.assertIsNone(updated.active_turn_id)
        self.assertEqual(updated.last_completed_turn_id, "turn-1")
        self.assertEqual(telegram.messages, [(22, "Reply from thread/read")])
        self.assertEqual(self.recorder.records, [("assistant", "Reply from thread/read")])

    def test_drain_codex_notifications_marks_auth_ready_after_login_completion(self) -> None:
        auth = AuthState(
            bot_token="token",
            telegram_user_id=11,
            telegram_chat_id=22,
            paired_at="now",
        )

        class Notification:
            def __init__(self, method: str, params: dict):
                self.method = method
                self.params = params

        persisted = CodexServerState(
            transport="stdio://",
            initialized=True,
            account_status="auth_required",
            auth_required=True,
            login_type="chatgpt",
            login_url="https://example.test/login",
        )
        save_codex_server_state(self.paths, persisted)
        self.runtime_state.codex_state = "AUTH_REQUIRED"

        telegram = FakeTelegramClient()
        codex = FakeCodex()
        codex.pending_notifications.append(
            Notification("account/updated", {"status": "ready", "accountType": "chatgpt"})
        )

        drain_codex_notifications(
            self.paths,
            auth,
            telegram,
            self.recorder,
            codex,
            self.runtime,
            self.runtime_state,
        )

        updated = load_codex_server_state(self.paths)
        self.assertIsNotNone(updated)
        assert updated is not None
        self.assertFalse(updated.auth_required)
        self.assertEqual(updated.account_status, "ready")
        self.assertEqual(updated.account_type, "chatgpt")
        self.assertIsNone(updated.login_url)
        self.assertIsNone(updated.login_type)
        self.assertEqual(self.runtime_state.codex_state, "RUNNING")
        self.assertEqual(telegram.messages, [])

    def test_drain_codex_notifications_flushes_partial_buffer_on_partial_event(self) -> None:
        auth = AuthState(
            bot_token="token",
            telegram_user_id=11,
            telegram_chat_id=22,
            paired_at="now",
        )
        store = SessionStore(self.paths)
        session = store.get_or_create_telegram_session(auth)
        session.thread_id = "thread-1"
        store.save_session(session)

        class Notification:
            def __init__(self, method: str, params: dict):
                self.method = method
                self.params = params

        telegram = FakeTelegramClient()
        codex = FakeCodex()
        codex.pending_notifications.append(Notification("assistant/message.delta", {"threadId": "thread-1", "text": "Hello"}))
        codex.pending_notifications.append(Notification("assistant/message.partial", {"threadId": "thread-1"}))

        drain_codex_notifications(self.paths, auth, telegram, self.recorder, codex)

        refreshed = store.get_or_create_telegram_session(auth)
        self.assertEqual(telegram.messages, [(22, "Hello")])
        self.assertEqual(self.recorder.records, [("assistant", "Hello")])
        self.assertEqual(refreshed.pending_output_text, "")

    def test_drain_codex_notifications_merges_buffer_with_final_completion(self) -> None:
        auth = AuthState(
            bot_token="token",
            telegram_user_id=11,
            telegram_chat_id=22,
            paired_at="now",
        )
        store = SessionStore(self.paths)
        session = store.get_or_create_telegram_session(auth)
        session.thread_id = "thread-1"
        session.active_turn_id = "turn-1"
        store.save_session(session)

        class Notification:
            def __init__(self, method: str, params: dict):
                self.method = method
                self.params = params

        telegram = FakeTelegramClient()
        codex = FakeCodex()
        codex.pending_notifications.append(Notification("assistant/message.delta", {"threadId": "thread-1", "text": "Hello "}))
        codex.pending_notifications.append(
            Notification("turn/completed", {"turnId": "turn-1", "outputText": "world"})
        )

        drain_codex_notifications(self.paths, auth, telegram, self.recorder, codex)

        updated = store.get_or_create_telegram_session(auth)
        self.assertIsNone(updated.active_turn_id)
        self.assertEqual(updated.pending_output_text, "")
        self.assertEqual(updated.last_completed_turn_id, "turn-1")
        self.assertEqual(telegram.messages, [(22, "Hello world")])
        self.assertEqual(self.recorder.records, [("assistant", "Hello world")])

    def test_turn_completed_for_detached_session_is_not_delivered_after_new(self) -> None:
        auth = AuthState(
            bot_token="token",
            telegram_user_id=11,
            telegram_chat_id=22,
            paired_at="now",
        )
        store = SessionStore(self.paths)
        original = store.get_or_create_telegram_session(auth)
        original.thread_id = "thread-old"
        original.active_turn_id = "turn-old"
        original.status = "RUNNING_TURN"
        store.save_session(original)
        active = store.create_new_telegram_session(auth)
        active.thread_id = "thread-new"
        store.save_session(active)

        class Notification:
            def __init__(self, method: str, params: dict):
                self.method = method
                self.params = params

        telegram = FakeTelegramClient()
        codex = FakeCodex()
        codex.pending_notifications.append(
            Notification("turn/completed", {"turnId": "turn-old", "outputText": "late answer"})
        )

        drain_codex_notifications(self.paths, auth, telegram, self.recorder, codex)

        sessions = store.list_telegram_sessions(auth)
        detached = next(session for session in sessions if session.session_id == original.session_id)
        self.assertFalse(detached.attached)
        self.assertEqual(detached.last_completed_turn_id, "turn-old")
        current = next(session for session in sessions if session.session_id == active.session_id)
        self.assertEqual(current.status, "ACTIVE")
        self.assertEqual(telegram.messages, [])
        self.assertEqual(self.recorder.records, [])
        self.assertTrue(any("hidden_session_output_consumed" in message for message in load_recovery_messages(self.paths)))

    def test_old_thread_delta_does_not_attach_to_new_active_session(self) -> None:
        auth = AuthState(
            bot_token="token",
            telegram_user_id=11,
            telegram_chat_id=22,
            paired_at="now",
        )
        store = SessionStore(self.paths)
        original = store.get_or_create_telegram_session(auth)
        original.thread_id = "thread-old"
        original.status = "RUNNING_TURN"
        store.save_session(original)
        active = store.create_new_telegram_session(auth)
        active.thread_id = "thread-new"
        store.save_session(active)

        class Notification:
            def __init__(self, method: str, params: dict):
                self.method = method
                self.params = params

        telegram = FakeTelegramClient()
        codex = FakeCodex()
        codex.pending_notifications.append(
            Notification("assistant/message.delta", {"threadId": "thread-old", "text": "late partial"})
        )
        codex.pending_notifications.append(Notification("assistant/message.partial", {"threadId": "thread-old"}))

        drain_codex_notifications(self.paths, auth, telegram, self.recorder, codex)

        self.assertEqual(telegram.messages, [])
        self.assertEqual(self.recorder.records, [])
        refreshed = store.list_telegram_sessions(auth)
        self.assertTrue(all(session.pending_output_text == "" for session in refreshed))

    def test_partial_output_persists_until_completion_after_restart_style_drain(self) -> None:
        auth = AuthState(
            bot_token="token",
            telegram_user_id=11,
            telegram_chat_id=22,
            paired_at="now",
        )
        store = SessionStore(self.paths)
        session = store.get_or_create_telegram_session(auth)
        session.thread_id = "thread-1"
        session.active_turn_id = "turn-1"
        session.status = "RUNNING_TURN"
        store.save_session(session)

        class Notification:
            def __init__(self, method: str, params: dict):
                self.method = method
                self.params = params

        first_codex = FakeCodex()
        first_codex.pending_notifications.append(
            Notification("assistant/message.delta", {"threadId": "thread-1", "text": "Hello "})
        )
        drain_codex_notifications(self.paths, auth, FakeTelegramClient(), self.recorder, first_codex)

        persisted = store.get_or_create_telegram_session(auth)
        self.assertEqual(persisted.pending_output_text, "Hello ")
        self.assertEqual(self.recorder.records, [])

        telegram = FakeTelegramClient()
        second_codex = FakeCodex()
        second_codex.pending_notifications.append(
            Notification("turn/completed", {"turnId": "turn-1", "outputText": "world"})
        )
        drain_codex_notifications(self.paths, auth, telegram, self.recorder, second_codex)

        updated = store.get_or_create_telegram_session(auth)
        self.assertIsNone(updated.active_turn_id)
        self.assertEqual(updated.pending_output_text, "")
        self.assertEqual(updated.last_completed_turn_id, "turn-1")
        self.assertEqual(telegram.messages, [(22, "Hello world")])
        self.assertEqual(self.recorder.records, [("assistant", "Hello world")])

    def test_duplicate_completed_turn_is_ignored_after_already_delivered(self) -> None:
        auth = AuthState(
            bot_token="token",
            telegram_user_id=11,
            telegram_chat_id=22,
            paired_at="now",
        )
        store = SessionStore(self.paths)
        session = store.get_or_create_telegram_session(auth)
        session.thread_id = "thread-1"
        session.active_turn_id = "turn-1"
        session.status = "RUNNING_TURN"
        store.save_session(session)

        class Notification:
            def __init__(self, method: str, params: dict):
                self.method = method
                self.params = params

        telegram = FakeTelegramClient()
        codex = FakeCodex()
        codex.pending_notifications.append(
            Notification("turn/completed", {"turnId": "turn-1", "outputText": "Final answer from Codex"})
        )
        codex.pending_notifications.append(
            Notification("turn/completed", {"turnId": "turn-1", "outputText": "Final answer from Codex"})
        )

        drain_codex_notifications(self.paths, auth, telegram, self.recorder, codex)

        updated = store.get_or_create_telegram_session(auth)
        self.assertEqual(updated.last_completed_turn_id, "turn-1")
        self.assertEqual(updated.last_delivered_output_text, "Final answer from Codex")
        self.assertEqual(telegram.messages, [(22, "Final answer from Codex")])
        self.assertEqual(self.recorder.records, [("assistant", "Final answer from Codex")])

    def test_late_notifications_for_completed_turn_are_ignored(self) -> None:
        auth = AuthState(
            bot_token="token",
            telegram_user_id=11,
            telegram_chat_id=22,
            paired_at="now",
        )
        store = SessionStore(self.paths)
        session = store.get_or_create_telegram_session(auth)
        session.thread_id = "thread-1"
        session.last_completed_turn_id = "turn-1"
        session.last_delivered_output_text = "Final answer from Codex"
        session.status = "ACTIVE"
        store.save_session(session)

        class Notification:
            def __init__(self, method: str, params: dict):
                self.method = method
                self.params = params

        telegram = FakeTelegramClient()
        codex = FakeCodex()
        codex.pending_notifications.extend(
            [
                Notification(
                    "item/commandExecution/outputDelta",
                    {
                        "threadId": "thread-1",
                        "turnId": "turn-1",
                        "itemId": "cmd-1",
                        "delta": 'rg -n "Emulator is connected but Android has not finished booting" -S /tmp 2>/dev/null',
                    },
                ),
                Notification(
                    "item/completed",
                    {
                        "threadId": "thread-1",
                        "turnId": "turn-1",
                        "item": {"type": "commandExecution", "id": "cmd-1", "command": "rg -n foo"},
                    },
                ),
            ]
        )

        drain_codex_notifications(self.paths, auth, telegram, self.recorder, codex)

        updated = store.get_or_create_telegram_session(auth)
        self.assertEqual(updated.last_delivered_output_text, "Final answer from Codex")
        self.assertEqual(updated.thinking_message_text, "")
        self.assertEqual(updated.thinking_history_by_source, {})
        self.assertEqual(telegram.messages, [])
        self.assertEqual(telegram.edits, [])

    def test_completed_turn_does_not_duplicate_matching_pending_full_answer(self) -> None:
        auth = AuthState(
            bot_token="token",
            telegram_user_id=11,
            telegram_chat_id=22,
            paired_at="now",
        )
        store = SessionStore(self.paths)
        session = store.get_or_create_telegram_session(auth)
        session.thread_id = "thread-1"
        session.active_turn_id = "turn-1"
        session.status = "RUNNING_TURN"
        store.save_session(session)

        class Notification:
            def __init__(self, method: str, params: dict):
                self.method = method
                self.params = params

        telegram = FakeTelegramClient()
        codex = FakeCodex()
        final_text = "I do not have human-style ongoing memory by default."
        codex.pending_notifications.extend(
            [
                Notification("assistant/message.delta", {"threadId": "thread-1", "text": final_text}),
                Notification("turn/completed", {"turnId": "turn-1", "outputText": final_text}),
            ]
        )

        drain_codex_notifications(self.paths, auth, telegram, self.recorder, codex)

        updated = store.get_or_create_telegram_session(auth)
        self.assertEqual(updated.last_completed_turn_id, "turn-1")
        self.assertEqual(updated.last_delivered_output_text, final_text)
        self.assertEqual(updated.pending_output_text, "")
        self.assertEqual(telegram.messages, [(22, "I do not have human-style ongoing memory by default.")])
        self.assertEqual(self.recorder.records, [("assistant", final_text)])

    def test_completed_turn_does_not_duplicate_matching_streamed_full_answer(self) -> None:
        auth = AuthState(
            bot_token="token",
            telegram_user_id=11,
            telegram_chat_id=22,
            paired_at="now",
        )
        store = SessionStore(self.paths)
        session = store.get_or_create_telegram_session(auth)
        session.thread_id = "thread-1"
        session.active_turn_id = "turn-1"
        session.status = "RUNNING_TURN"
        session.streaming_message_id = 1
        session.streaming_output_text = "Final answer from Codex"
        session.last_delivered_output_text = "Final answer from Codex"
        store.save_session(session)

        class Notification:
            def __init__(self, method: str, params: dict):
                self.method = method
                self.params = params

        telegram = FakeTelegramClient()
        codex = FakeCodex()
        codex.pending_notifications.append(
            Notification("turn/completed", {"turnId": "turn-1", "outputText": "Final answer from Codex"})
        )

        drain_codex_notifications(self.paths, auth, telegram, self.recorder, codex)

        updated = store.get_or_create_telegram_session(auth)
        self.assertEqual(updated.last_completed_turn_id, "turn-1")
        self.assertEqual(updated.pending_output_text, "")
        self.assertEqual(updated.streaming_output_text, "")
        self.assertEqual(telegram.messages, [])
        self.assertEqual(telegram.edits, [])
        self.assertEqual(self.recorder.records, [])

    def test_duplicate_partial_flush_with_same_text_is_ignored(self) -> None:
        auth = AuthState(
            bot_token="token",
            telegram_user_id=11,
            telegram_chat_id=22,
            paired_at="now",
        )
        store = SessionStore(self.paths)
        session = store.get_or_create_telegram_session(auth)
        session.thread_id = "thread-1"
        session.status = "RUNNING_TURN"
        store.save_session(session)

        class Notification:
            def __init__(self, method: str, params: dict):
                self.method = method
                self.params = params

        telegram = FakeTelegramClient()
        first_codex = FakeCodex()
        first_codex.pending_notifications.append(
            Notification("assistant/message.delta", {"threadId": "thread-1", "text": "Hello"})
        )
        first_codex.pending_notifications.append(Notification("assistant/message.partial", {"threadId": "thread-1"}))
        drain_codex_notifications(self.paths, auth, telegram, self.recorder, first_codex)

        second_codex = FakeCodex()
        second_codex.pending_notifications.append(
            Notification("assistant/message.delta", {"threadId": "thread-1", "text": "Hello"})
        )
        second_codex.pending_notifications.append(Notification("assistant/message.partial", {"threadId": "thread-1"}))
        drain_codex_notifications(self.paths, auth, telegram, self.recorder, second_codex)

        updated = store.get_or_create_telegram_session(auth)
        self.assertEqual(updated.last_delivered_output_text, "Hello")
        self.assertEqual(updated.pending_output_text, "")
        self.assertEqual(telegram.messages, [(22, "Hello")])
        self.assertEqual(self.recorder.records, [("assistant", "Hello")])

    def test_partial_stream_is_edited_in_place_until_completion(self) -> None:
        auth = AuthState(
            bot_token="token",
            telegram_user_id=11,
            telegram_chat_id=22,
            paired_at="now",
        )
        store = SessionStore(self.paths)
        session = store.get_or_create_telegram_session(auth)
        session.thread_id = "thread-1"
        session.active_turn_id = "turn-1"
        session.status = "RUNNING_TURN"
        store.save_session(session)

        class Notification:
            def __init__(self, method: str, params: dict):
                self.method = method
                self.params = params

        telegram = FakeTelegramClient()
        codex = FakeCodex()
        codex.pending_notifications.extend(
            [
                Notification("assistant/message.delta", {"threadId": "thread-1", "text": "Hello"}),
                Notification("assistant/message.partial", {"threadId": "thread-1"}),
                Notification("assistant/message.delta", {"threadId": "thread-1", "text": " world"}),
                Notification("assistant/message.partial", {"threadId": "thread-1"}),
                Notification("turn/completed", {"turnId": "turn-1", "outputText": "!"}),
            ]
        )

        drain_codex_notifications(self.paths, auth, telegram, self.recorder, codex)

        updated = store.get_or_create_telegram_session(auth)
        self.assertEqual(telegram.messages, [(22, "Hello")])
        self.assertEqual(telegram.edits, [(22, 1, "Hello world!")])
        self.assertEqual(updated.streaming_message_id, None)
        self.assertEqual(updated.streaming_output_text, "")
        self.assertEqual(updated.last_delivered_output_text, "Hello world!")

    def test_partial_stream_is_buffered_until_final_reply(self) -> None:
        auth = AuthState(
            bot_token="token",
            telegram_user_id=11,
            telegram_chat_id=22,
            paired_at="now",
        )
        store = SessionStore(self.paths)
        session = store.get_or_create_telegram_session(auth)
        session.thread_id = "thread-1"
        session.active_turn_id = "turn-1"
        session.status = "RUNNING_TURN"
        store.save_session(session)

        class Notification:
            def __init__(self, method: str, params: dict):
                self.method = method
                self.params = params

        telegram = FakeTelegramClient()
        codex = FakeCodex()
        codex.pending_notifications.extend(
            [
                Notification("assistant/message.delta", {"threadId": "thread-1", "text": "Hello *world"}),
                Notification("assistant/message.delta", {"threadId": "thread-1", "text": " - ok!"}),
            ]
        )

        drain_codex_notifications(self.paths, auth, telegram, self.recorder, codex)

        self.assertEqual(telegram.messages, [])
        updated = store.get_or_create_telegram_session(auth)
        self.assertEqual(updated.pending_output_text, "Hello *world - ok!")
        self.assertEqual(updated.last_delivered_output_text, "")

    def test_thinking_updates_send_independent_html_message(self) -> None:
        auth = AuthState(
            bot_token="token",
            telegram_user_id=11,
            telegram_chat_id=22,
            paired_at="now",
        )
        store = SessionStore(self.paths)
        session = store.get_or_create_telegram_session(auth)
        session.thread_id = "thread-1"
        session.active_turn_id = "turn-1"
        session.status = "RUNNING_TURN"
        store.save_session(session)

        class Notification:
            def __init__(self, method: str, params: dict):
                self.method = method
                self.params = params

        telegram = FakeTelegramClient()
        codex = FakeCodex()
        codex.pending_notifications.append(
            Notification(
                "item/updated",
                {"threadId": "thread-1", "item": {"type": "reasoning", "text": "Checking recent release notes..."}},
            )
        )

        drain_codex_notifications(self.paths, auth, telegram, self.recorder, codex)

        self.assertEqual(
            telegram.message_details,
            [(22, "Checking recent release notes...", None, "HTML", True)],
        )

    def test_partial_answer_updates_do_not_send_intermediate_messages(self) -> None:
        auth = AuthState(
            bot_token="token",
            telegram_user_id=11,
            telegram_chat_id=22,
            paired_at="now",
        )
        store = SessionStore(self.paths)
        session = store.get_or_create_telegram_session(auth)
        session.thread_id = "thread-1"
        session.active_turn_id = "turn-1"
        session.status = "RUNNING_TURN"
        store.save_session(session)

        class Notification:
            def __init__(self, method: str, params: dict):
                self.method = method
                self.params = params

        telegram = FakeTelegramClient()
        codex = FakeCodex()
        codex.pending_notifications.extend(
            [
                Notification("assistant/message.delta", {"threadId": "thread-1", "text": "Hello"}),
                Notification("assistant/message.delta", {"threadId": "thread-1", "text": " world"}),
            ]
        )

        drain_codex_notifications(self.paths, auth, telegram, self.recorder, codex)

        self.assertEqual(telegram.messages, [])
        self.assertEqual(telegram.edits, [])
        updated = store.get_or_create_telegram_session(auth)
        self.assertEqual(updated.pending_output_text, "Hello world")

    def test_final_reply_uses_telegram_html(self) -> None:
        auth = AuthState(
            bot_token="token",
            telegram_user_id=11,
            telegram_chat_id=22,
            paired_at="now",
        )
        store = SessionStore(self.paths)
        session = store.get_or_create_telegram_session(auth)
        session.thread_id = "thread-1"
        session.active_turn_id = "turn-1"
        session.status = "RUNNING_TURN"
        store.save_session(session)

        class Notification:
            def __init__(self, method: str, params: dict):
                self.method = method
                self.params = params

        class HtmlRecordingTelegram(FakeTelegramClient):
            def __init__(self):
                super().__init__()
                self.message_calls: list[tuple[int, str, int | None, str | None]] = []

            def send_message(self, chat_id: int, text: str, topic_id: int | None = None, parse_mode: str | None = None) -> dict:
                self.message_calls.append((chat_id, text, topic_id, parse_mode))
                return super().send_message(chat_id, text, topic_id=topic_id, parse_mode=parse_mode)

        telegram = HtmlRecordingTelegram()
        codex = FakeCodex()
        codex.pending_notifications.append(
            Notification("turn/completed", {"turnId": "turn-1", "outputText": "# Title\n**bold**"})
        )

        drain_codex_notifications(self.paths, auth, telegram, self.recorder, codex)

        self.assertEqual(len(telegram.message_calls), 1)
        self.assertEqual(telegram.message_calls[0][3], "HTML")
        self.assertEqual(telegram.messages, [(22, "<b>Title</b>\n<b>bold</b>")])

    def test_final_reply_falls_back_to_escaped_html_when_html_send_fails(self) -> None:
        auth = AuthState(
            bot_token="token",
            telegram_user_id=11,
            telegram_chat_id=22,
            paired_at="now",
        )
        store = SessionStore(self.paths)
        session = store.get_or_create_telegram_session(auth)
        session.thread_id = "thread-1"
        session.active_turn_id = "turn-1"
        session.status = "RUNNING_TURN"
        store.save_session(session)

        class Notification:
            def __init__(self, method: str, params: dict):
                self.method = method
                self.params = params

        class FallbackTelegram(FakeTelegramClient):
            def __init__(self):
                super().__init__()
                self.parse_modes: list[str | None] = []

            def send_message(self, chat_id: int, text: str, topic_id: int | None = None, parse_mode: str | None = None) -> dict:
                self.parse_modes.append(parse_mode)
                if parse_mode == "HTML" and text == "<b>Title</b>\n<b>bold</b>":
                    raise TelegramError("can't parse entities")
                return super().send_message(chat_id, text, topic_id=topic_id, parse_mode=parse_mode)

        telegram = FallbackTelegram()
        codex = FakeCodex()
        codex.pending_notifications.append(
            Notification("turn/completed", {"turnId": "turn-1", "outputText": "# Title\n**bold**"})
        )

        drain_codex_notifications(self.paths, auth, telegram, self.recorder, codex)

        self.assertEqual(telegram.parse_modes, ["HTML", "HTML"])
        self.assertEqual(telegram.messages, [(22, "# Title\n**bold**")])

    def test_final_reply_does_not_edit_existing_stream_message(self) -> None:
        auth = AuthState(
            bot_token="token",
            telegram_user_id=11,
            telegram_chat_id=22,
            paired_at="now",
        )
        store = SessionStore(self.paths)
        session = store.get_or_create_telegram_session(auth)
        session.thread_id = "thread-1"
        session.active_turn_id = "turn-1"
        session.status = "RUNNING_TURN"
        store.save_session(session)
        telegram = FakeTelegramClient()
        session.pending_output_text = "# Title\n**bold**"
        store.save_session(session)

        flush_buffer(
            session.session_id,
            auth,
            telegram,
            self.recorder,
            store,
            mark_agent=True,
        )

        self.assertEqual(telegram.edits, [])
        self.assertEqual(telegram.messages, [(22, "<b>Title</b>\n<b>bold</b>")])

    def test_final_reply_passes_through_existing_telegram_html(self) -> None:
        auth = AuthState(
            bot_token="token",
            telegram_user_id=11,
            telegram_chat_id=22,
            paired_at="now",
        )
        store = SessionStore(self.paths)
        session = store.get_or_create_telegram_session(auth)
        session.thread_id = "thread-1"
        session.active_turn_id = "turn-1"
        session.status = "RUNNING_TURN"
        store.save_session(session)

        class Notification:
            def __init__(self, method: str, params: dict):
                self.method = method
                self.params = params

        telegram = FakeTelegramClient()
        codex = FakeCodex()
        text = "I’m *Tele Cli* \\- your Telegram\\-first assistant running on your own device\\."
        codex.pending_notifications.append(
            Notification("turn/completed", {"turnId": "turn-1", "outputText": text})
        )

        drain_codex_notifications(self.paths, auth, telegram, self.recorder, codex)

        self.assertEqual(telegram.messages, [(22, to_telegram_html(normalize_telegram_markdown_source(text)))])
        self.assertNotIn("telegram.format_failure", load_event_types(self.paths))

    def test_final_reply_normalizes_mixed_existing_telegram_markdownv2(self) -> None:
        auth = AuthState(
            bot_token="token",
            telegram_user_id=11,
            telegram_chat_id=22,
            paired_at="now",
        )
        store = SessionStore(self.paths)
        session = store.get_or_create_telegram_session(auth)
        session.thread_id = "thread-1"
        session.active_turn_id = "turn-1"
        session.status = "RUNNING_TURN"
        store.save_session(session)

        class Notification:
            def __init__(self, method: str, params: dict):
                self.method = method
                self.params = params

        class MixedTelegram(FakeTelegramClient):
            def __init__(self):
                super().__init__()
                self.parse_modes: list[str | None] = []

            def send_message(self, chat_id: int, text: str, topic_id: int | None = None, parse_mode: str | None = None) -> dict:
                self.parse_modes.append(parse_mode)
                if parse_mode == "MarkdownV2" and text.startswith("I’m *Tele Cli* —"):
                    raise TelegramError("can't parse entities")
                return super().send_message(chat_id, text, topic_id=topic_id, parse_mode=parse_mode)

        telegram = MixedTelegram()
        codex = FakeCodex()
        text = (
            "I’m *Tele Cli* — your Telegram\\-first personal assistant running on your own device.\n\n"
            "- one\n"
            "- two"
        )
        codex.pending_notifications.append(
            Notification("turn/completed", {"turnId": "turn-1", "outputText": text})
        )

        drain_codex_notifications(self.paths, auth, telegram, self.recorder, codex)

        self.assertEqual(telegram.parse_modes, ["HTML"])
        self.assertEqual(telegram.messages, [(22, to_telegram_html(normalize_telegram_markdown_source(text)))])

    def test_final_reply_uses_markdown_code_block_emergency_fallback_and_logs_failure(self) -> None:
        auth = AuthState(
            bot_token="token",
            telegram_user_id=11,
            telegram_chat_id=22,
            paired_at="now",
        )
        store = SessionStore(self.paths)
        session = store.get_or_create_telegram_session(auth)
        session.thread_id = "thread-1"
        session.active_turn_id = "turn-1"
        session.status = "RUNNING_TURN"
        store.save_session(session)

        class Notification:
            def __init__(self, method: str, params: dict):
                self.method = method
                self.params = params

        class EmergencyFallbackTelegram(FakeTelegramClient):
            def __init__(self):
                super().__init__()
                self.parse_modes: list[str | None] = []

            def send_message(self, chat_id: int, text: str, topic_id: int | None = None, parse_mode: str | None = None) -> dict:
                self.parse_modes.append(parse_mode)
                if parse_mode == "MarkdownV2" and not text.startswith("```"):
                    raise TelegramError("can't parse entities")
                return super().send_message(chat_id, text, topic_id=topic_id, parse_mode=parse_mode)

        telegram = EmergencyFallbackTelegram()
        codex = FakeCodex()
        codex.pending_notifications.append(
            Notification("turn/completed", {"turnId": "turn-1", "outputText": "I’m *Tele Cli* - assistant."})
        )

        drain_codex_notifications(self.paths, auth, telegram, self.recorder, codex)

        self.assertNotIn("telegram.format_failure", load_event_types(self.paths))
        self.assertEqual(telegram.parse_modes, ["HTML"])
        self.assertEqual(
            telegram.messages,
            [(22, to_telegram_html(normalize_telegram_markdown_source("I’m *Tele Cli* - assistant.")))],
        )

    def test_turn_completed_does_not_duplicate_item_completed_agent_message(self) -> None:
        auth = AuthState(
            bot_token="token",
            telegram_user_id=11,
            telegram_chat_id=22,
            paired_at="now",
        )
        store = SessionStore(self.paths)
        session = store.get_or_create_telegram_session(auth)
        session.thread_id = "thread-1"
        session.active_turn_id = "turn-1"
        session.status = "RUNNING_TURN"
        store.save_session(session)

        class Notification:
            def __init__(self, method: str, params: dict):
                self.method = method
                self.params = params

        class ThreadReadingCodex(FakeCodex):
            def read_thread(self, thread_id: str, include_turns: bool = True):
                return {
                    "thread": {
                        "turns": [
                            {
                                "items": [
                                    {
                                        "type": "agentMessage",
                                        "text": "Final answer",
                                    }
                                ]
                            }
                        ]
                    }
                }

        telegram = FakeTelegramClient()
        codex = ThreadReadingCodex()
        codex.pending_notifications.extend(
            [
                Notification(
                    "item/completed",
                    {
                        "threadId": "thread-1",
                        "turnId": "turn-1",
                        "item": {"type": "agentMessage", "text": "Final answer", "phase": "final_answer"},
                    },
                ),
                Notification("turn/completed", {"threadId": "thread-1", "turn": {"id": "turn-1"}}),
            ]
        )

        drain_codex_notifications(self.paths, auth, telegram, self.recorder, codex)

        updated = store.get_or_create_telegram_session(auth)
        self.assertEqual(telegram.messages, [(22, "Final answer")])
        self.assertEqual(updated.last_delivered_output_text, "Final answer")

    def test_item_completed_full_snapshot_replaces_streamed_agent_message(self) -> None:
        auth = AuthState(
            bot_token="token",
            telegram_user_id=11,
            telegram_chat_id=22,
            paired_at="now",
        )
        store = SessionStore(self.paths)
        session = store.get_or_create_telegram_session(auth)
        session.thread_id = "thread-1"
        session.active_turn_id = "turn-1"
        session.status = "RUNNING_TURN"
        session.streaming_message_id = 1
        session.thinking_message_text = "Thinking..."
        store.save_session(session)

        class Notification:
            def __init__(self, method: str, params: dict):
                self.method = method
                self.params = params

        final_text = (
            "I’m a coding-focused AI assistant working with you directly in this workspace.\n\n"
            "I can inspect the repo, edit files, run commands, debug issues, review code, and explain technical tradeoffs."
        )
        codex = FakeCodex()
        codex.pending_notifications.extend(
            [
                Notification("item/agentMessage/delta", {"threadId": "thread-1", "turnId": "turn-1", "delta": "I’m a coding-focused AI assistant working with you directly in this workspace.\n\n"}),
                Notification("item/agentMessage/delta", {"threadId": "thread-1", "turnId": "turn-1", "delta": " can inspect the repo, edit files, run commands, debug issues, review code, and explain technical tradeoffs."}),
                Notification(
                    "item/completed",
                    {
                        "threadId": "thread-1",
                        "turnId": "turn-1",
                        "item": {"type": "agentMessage", "text": final_text, "phase": "final_answer"},
                    },
                ),
                Notification("turn/completed", {"threadId": "thread-1", "turn": {"id": "turn-1"}}),
            ]
        )
        telegram = FakeTelegramClient()

        drain_codex_notifications(self.paths, auth, telegram, self.recorder, codex)

        updated = store.get_or_create_telegram_session(auth)
        self.assertEqual(updated.last_delivered_output_text, final_text)
        self.assertEqual(updated.pending_output_text, "")
        self.assertEqual(updated.streaming_output_text, "")
        self.assertEqual(telegram.edits[-1], (22, 1, final_text))

    def test_cumulative_assistant_message_deltas_do_not_duplicate_output(self) -> None:
        auth = AuthState(
            bot_token="token",
            telegram_user_id=11,
            telegram_chat_id=22,
            paired_at="now",
        )
        store = SessionStore(self.paths)
        session = store.get_or_create_telegram_session(auth)
        session.thread_id = "thread-1"
        session.active_turn_id = "turn-1"
        session.status = "RUNNING_TURN"
        store.save_session(session)

        class Notification:
            def __init__(self, method: str, params: dict):
                self.method = method
                self.params = params

        telegram = FakeTelegramClient()
        codex = FakeCodex()
        codex.pending_notifications.extend(
            [
                Notification("assistant/message.delta", {"threadId": "thread-1", "text": "Hello"}),
                Notification("assistant/message.delta", {"threadId": "thread-1", "text": "Hello there"}),
                Notification("assistant/message.delta", {"threadId": "thread-1", "text": "Hello there friend"}),
                Notification("turn/completed", {"turnId": "turn-1", "outputText": "Hello there friend"}),
            ]
        )

        drain_codex_notifications(self.paths, auth, telegram, self.recorder, codex)

        updated = store.get_or_create_telegram_session(auth)
        self.assertEqual(updated.last_delivered_output_text, "Hello there friend")
        self.assertEqual(updated.pending_output_text, "")
        self.assertEqual(updated.streaming_output_text, "")
        self.assertEqual(telegram.messages, [(22, "Hello there friend")])

    def test_revised_full_assistant_message_delta_replaces_previous_snapshot(self) -> None:
        auth = AuthState(
            bot_token="token",
            telegram_user_id=11,
            telegram_chat_id=22,
            paired_at="now",
        )
        store = SessionStore(self.paths)
        session = store.get_or_create_telegram_session(auth)
        session.thread_id = "thread-1"
        session.active_turn_id = "turn-1"
        session.status = "RUNNING_TURN"
        store.save_session(session)

        class Notification:
            def __init__(self, method: str, params: dict):
                self.method = method
                self.params = params

        first_text = (
            "I\u2019m your coding agent in this workspace. I can inspect the repo, edit files, run commands.\n\n"
            " default to being practical: understand the code first, make the change, test what I can."
        )
        revised_text = (
            "I\u2019m your coding agent in this workspace. I can inspect the repo, edit files, run commands.\n\n"
            "I default to being practical: understand the code first, make the change, test what I can."
        )
        telegram = FakeTelegramClient()
        codex = FakeCodex()
        codex.pending_notifications.extend(
            [
                Notification("assistant/message.delta", {"threadId": "thread-1", "text": first_text}),
                Notification("assistant/message.delta", {"threadId": "thread-1", "text": revised_text}),
                Notification("turn/completed", {"turnId": "turn-1", "outputText": revised_text}),
            ]
        )

        drain_codex_notifications(self.paths, auth, telegram, self.recorder, codex)

        updated = store.get_or_create_telegram_session(auth)
        self.assertEqual(updated.last_delivered_output_text, revised_text)
        self.assertEqual(updated.pending_output_text, "")
        self.assertEqual(updated.streaming_output_text, "")
        self.assertEqual(telegram.messages, [(22, revised_text)])

    def test_cumulative_item_agent_message_deltas_edit_in_place_without_duplication(self) -> None:
        auth = AuthState(
            bot_token="token",
            telegram_user_id=11,
            telegram_chat_id=22,
            paired_at="now",
        )
        store = SessionStore(self.paths)
        session = store.get_or_create_telegram_session(auth)
        session.thread_id = "thread-1"
        session.active_turn_id = "turn-1"
        session.status = "RUNNING_TURN"
        session.streaming_message_id = 1
        session.thinking_message_text = "Thinking..."
        store.save_session(session)

        class Notification:
            def __init__(self, method: str, params: dict):
                self.method = method
                self.params = params

        telegram = FakeTelegramClient()
        codex = FakeCodex()
        codex.pending_notifications.extend(
            [
                Notification("item/agentMessage/delta", {"threadId": "thread-1", "turnId": "turn-1", "delta": "Hello"}),
                Notification(
                    "item/agentMessage/delta",
                    {"threadId": "thread-1", "turnId": "turn-1", "delta": "Hello there"},
                ),
                Notification(
                    "item/agentMessage/delta",
                    {"threadId": "thread-1", "turnId": "turn-1", "delta": "Hello there friend"},
                ),
                Notification("turn/completed", {"turnId": "turn-1", "outputText": "Hello there friend"}),
            ]
        )

        drain_codex_notifications(self.paths, auth, telegram, self.recorder, codex)

        updated = store.get_or_create_telegram_session(auth)
        self.assertEqual(updated.last_delivered_output_text, "Hello there friend")
        self.assertEqual(updated.pending_output_text, "")
        self.assertEqual(updated.streaming_output_text, "")
        self.assertEqual(telegram.edits[-1], (22, 1, "Hello there friend"))

    def test_final_reply_is_chunked_when_placeholder_edit_is_too_large(self) -> None:
        auth = AuthState(
            bot_token="token",
            telegram_user_id=11,
            telegram_chat_id=22,
            paired_at="now",
        )
        store = SessionStore(self.paths)
        session = store.get_or_create_telegram_session(auth)
        session.thread_id = "thread-1"
        session.active_turn_id = "turn-1"
        session.status = "RUNNING_TURN"
        session.streaming_message_id = 1
        session.thinking_message_text = "Thinking..."
        session.pending_output_text = "A" * 5000
        store.save_session(session)

        class FailingEditTelegram(FakeTelegramClient):
            def edit_message_text(self, chat_id: int, message_id: int, text: str) -> dict:
                if len(text) > 4000:
                    raise TelegramError("HTTP Error 400: Bad Request")
                return super().edit_message_text(chat_id, message_id, text)

        telegram = FailingEditTelegram()

        flush_buffer(
            session.session_id,
            auth,
            telegram,
            self.recorder,
            store,
            mark_agent=True,
        )

        updated = store.get_or_create_telegram_session(auth)
        self.assertEqual(len(telegram.edits), 1)
        self.assertEqual(len(telegram.edits[0][2]), 4000)
        self.assertEqual(len(telegram.messages), 1)
        self.assertEqual(len(telegram.messages[0][1]), 1000)
        self.assertEqual(updated.last_delivered_output_text, "A" * 5000)

    def test_flush_buffer_routes_group_session_output_to_session_chat(self) -> None:
        auth = AuthState(
            bot_token="token",
            telegram_user_id=11,
            telegram_chat_id=22,
            paired_at="now",
        )
        store = SessionStore(self.paths)
        session = store.get_or_create_telegram_session(auth)
        session.transport_chat_id = 44
        session.transport_topic_id = 77
        session.thread_id = "thread-1"
        session.active_turn_id = "turn-1"
        session.status = "RUNNING_TURN"
        session.streaming_message_id = 1
        session.thinking_message_text = "Thinking..."
        session.pending_output_text = "Hello group"
        store.save_session(session)
        telegram = FakeTelegramClient()

        flush_buffer(
            session.session_id,
            auth,
            telegram,
            self.recorder,
            store,
            mark_agent=False,
        )

        updated = store.find_by_thread_id("thread-1")
        self.assertIsNotNone(updated)
        assert updated is not None
        self.assertEqual(telegram.edits, [(44, 1, "Hello group")])
        self.assertEqual(telegram.messages, [])
        self.assertEqual(updated.last_delivered_output_text, "Hello group")

    def test_ensure_thinking_message_sends_placeholder(self) -> None:
        auth = AuthState(
            bot_token="token",
            telegram_user_id=11,
            telegram_chat_id=22,
            paired_at="now",
        )
        store = SessionStore(self.paths)
        session = store.get_or_create_telegram_session(auth)
        session.thread_id = "thread-1"
        session.active_turn_id = "turn-1"
        store.save_session(session)
        telegram = FakeTelegramClient()

        ensure_thinking_message(auth, telegram, session, text="Thinking")
        store.save_session(session)

        updated = store.get_or_create_telegram_session(auth)
        self.assertEqual(telegram.messages, [])
        self.assertIsNone(updated.streaming_message_id)
        self.assertEqual(updated.thinking_message_text, "Thinking")
        self.assertEqual(updated.streaming_output_text, "")

    def test_ensure_thinking_message_routes_to_session_chat(self) -> None:
        auth = AuthState(
            bot_token="token",
            telegram_user_id=11,
            telegram_chat_id=22,
            paired_at="now",
        )
        store = SessionStore(self.paths)
        session = store.get_or_create_telegram_session(auth)
        session.transport_chat_id = 44
        session.transport_topic_id = 77
        session.thread_id = "thread-1"
        session.active_turn_id = "turn-1"
        store.save_session(session)
        telegram = FakeTelegramClient()

        ensure_thinking_message(auth, telegram, session, text="Thinking")
        store.save_session(session)

        updated = store.find_by_thread_id("thread-1")
        self.assertIsNotNone(updated)
        assert updated is not None
        self.assertEqual(telegram.messages, [])
        self.assertIsNone(updated.streaming_message_id)
        self.assertEqual(updated.thinking_message_text, "Thinking")
        self.assertEqual(updated.streaming_output_text, "")

    def test_maybe_refresh_thinking_message_edits_placeholder(self) -> None:
        auth = AuthState(
            bot_token="token",
            telegram_user_id=11,
            telegram_chat_id=22,
            paired_at="now",
        )
        store = SessionStore(self.paths)
        session = store.get_or_create_telegram_session(auth)
        session.thread_id = "thread-1"
        session.active_turn_id = "turn-1"
        session.streaming_message_id = 1
        session.thinking_message_text = "Thinking"
        session.last_user_message_at = (datetime.now(timezone.utc) - timedelta(seconds=15)).isoformat()
        store.save_session(session)
        telegram = FakeTelegramClient()

        maybe_refresh_thinking_message(self.paths, auth, telegram, store)

        updated = store.get_or_create_telegram_session(auth)
        self.assertEqual(telegram.edits, [])
        self.assertEqual(updated.thinking_message_text, "Thinking")

    def test_maybe_refresh_thinking_message_skips_stale_active_turn(self) -> None:
        auth = AuthState(
            bot_token="token",
            telegram_user_id=11,
            telegram_chat_id=22,
            paired_at="now",
        )
        store = SessionStore(self.paths)
        session = store.get_or_create_telegram_session(auth)
        session.thread_id = "thread-1"
        session.active_turn_id = "turn-1"
        session.status = "RUNNING_TURN"
        session.streaming_message_id = 1
        session.thinking_message_text = "Thinking"
        session.last_user_message_at = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        store.save_session(session)
        telegram = FakeTelegramClient()

        maybe_refresh_thinking_message(self.paths, auth, telegram, store)

        updated = store.get_or_create_telegram_session(auth)
        self.assertEqual(telegram.edits, [])
        self.assertEqual(updated.thinking_message_text, "Thinking")

    def test_drain_codex_notifications_surfaces_reasoning_text_before_answer(self) -> None:
        auth = AuthState(
            bot_token="token",
            telegram_user_id=11,
            telegram_chat_id=22,
            paired_at="now",
        )
        store = SessionStore(self.paths)
        session = store.get_or_create_telegram_session(auth)
        session.thread_id = "thread-1"
        session.active_turn_id = "turn-1"
        session.status = "RUNNING_TURN"
        store.save_session(session)

        class Notification:
            def __init__(self, method: str, params: dict):
                self.method = method
                self.params = params

        telegram = FakeTelegramClient()
        codex = FakeCodex()
        codex.pending_notifications.append(
            Notification(
                "item/updated",
                {"threadId": "thread-1", "item": {"type": "reasoning", "text": "Checking recent release notes..."}},
            )
        )

        drain_codex_notifications(self.paths, auth, telegram, self.recorder, codex)

        updated = store.get_or_create_telegram_session(auth)
        self.assertEqual(telegram.messages, [(22, "Checking recent release notes...")])
        self.assertEqual(updated.thinking_message_text, "Checking recent release notes...")
        self.assertEqual(updated.streaming_output_text, "")

    def test_drain_codex_notifications_surfaces_command_activity(self) -> None:
        auth = AuthState(
            bot_token="token",
            telegram_user_id=11,
            telegram_chat_id=22,
            paired_at="now",
        )
        store = SessionStore(self.paths)
        session = store.get_or_create_telegram_session(auth)
        session.thread_id = "thread-1"
        session.active_turn_id = "turn-1"
        session.status = "RUNNING_TURN"
        store.save_session(session)

        class Notification:
            def __init__(self, method: str, params: dict):
                self.method = method
                self.params = params

        telegram = FakeTelegramClient()
        codex = FakeCodex()
        codex.pending_notifications.append(
            Notification(
                "item/started",
                {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "item": {"type": "commandExecution", "command": "git status --short", "status": "inProgress"},
                },
            )
        )

        drain_codex_notifications(self.paths, auth, telegram, self.recorder, codex)

        updated = store.get_or_create_telegram_session(auth)
        self.assertEqual(telegram.messages, [(22, '<pre><code class="language-bash">git status --short</code></pre>')])
        self.assertEqual(updated.thinking_message_text, '<pre><code class="language-bash">git status --short</code></pre>')

    def test_extract_activity_text_from_search_tool(self) -> None:
        text = extract_activity_text(
            "item/started",
            {
                "item": {
                    "type": "dynamicToolCall",
                    "tool": "search",
                    "arguments": {"query": "latest codex releases"},
                }
            },
        )

        self.assertEqual(text, "Searching: latest codex releases")

    def test_extract_activity_text_unwraps_shell_command_wrapper(self) -> None:
        text = extract_activity_text(
            "item/started",
            {
                "item": {
                    "type": "commandExecution",
                    "command": '/bin/zsh -lc "sed -n \'1,40p\' \\"$HOME/project/file.ts\\""',
                }
            },
        )

        self.assertEqual(text, '__tele_cli_command__:sed -n \'1,40p\' \\"$HOME/project/file.ts\\"')

    def test_extract_activity_text_from_command_output_delta(self) -> None:
        text = extract_activity_text(
            "item/commandExecution/outputDelta",
            {
                "delta": "cloning into repository...",
            },
        )

        self.assertEqual(text, "cloning into repository...")

    def test_extract_activity_text_from_file_change_output_delta(self) -> None:
        text = extract_activity_text(
            "item/fileChange/outputDelta",
            {
                "delta": "updated README.md",
            },
        )

        self.assertEqual(text, "Applying file changes: updated README.md")

    def test_extract_activity_text_from_plan_delta(self) -> None:
        text = extract_activity_text(
            "item/plan/delta",
            {
                "delta": "inspect the auth flow first",
            },
        )

        self.assertEqual(text, "Planning: inspect the auth flow first")

    def test_extract_event_driven_status_from_agent_message_delta(self) -> None:
        text = extract_event_driven_status("item/agentMessage/delta", {})
        self.assertIsNone(text)

    def test_extract_event_driven_status_from_thread_status_changed(self) -> None:
        text = extract_event_driven_status("thread/status/changed", {"status": {"type": "active"}})
        self.assertIsNone(text)

    def test_extract_event_driven_status_from_thread_status_changed_idle(self) -> None:
        text = extract_event_driven_status("thread/status/changed", {"status": {"type": "idle"}})
        self.assertIsNone(text)

    def test_extract_event_driven_status_from_thread_status_waiting_flag(self) -> None:
        text = extract_event_driven_status(
            "thread/status/changed",
            {"status": {"type": "active", "activeFlags": ["waitingOnApproval"]}},
        )
        self.assertEqual(text, "Waiting On Approval")

    def test_extract_event_driven_status_from_server_request_resolved(self) -> None:
        text = extract_event_driven_status("serverRequest/resolved", {"threadId": "thread-1", "requestId": 8})
        self.assertIsNone(text)

    def test_extract_event_driven_status_from_thread_token_usage_updated(self) -> None:
        text = extract_event_driven_status("thread/tokenUsage/updated", {"threadId": "thread-1", "turnId": "turn-1"})
        self.assertIsNone(text)

    def test_non_default_thinking_text_is_not_overwritten_by_idle_refresh(self) -> None:
        auth = AuthState(
            bot_token="token",
            telegram_user_id=11,
            telegram_chat_id=22,
            paired_at="now",
        )
        store = SessionStore(self.paths)
        session = store.get_or_create_telegram_session(auth)
        session.thread_id = "thread-1"
        session.active_turn_id = "turn-1"
        session.streaming_message_id = 1
        session.thinking_message_text = "Running command: git status --short"
        session.last_user_message_at = (datetime.now(timezone.utc) - timedelta(seconds=20)).isoformat()
        store.save_session(session)
        telegram = FakeTelegramClient()

        maybe_refresh_thinking_message(self.paths, auth, telegram, store)

        updated = store.get_or_create_telegram_session(auth)
        self.assertEqual(telegram.edits, [])
        self.assertEqual(updated.thinking_message_text, "Running command: git status --short")

    def test_drain_codex_notifications_appends_reasoning_text_delta(self) -> None:
        auth = AuthState(
            bot_token="token",
            telegram_user_id=11,
            telegram_chat_id=22,
            paired_at="now",
        )
        store = SessionStore(self.paths)
        session = store.get_or_create_telegram_session(auth)
        session.thread_id = "thread-1"
        session.active_turn_id = "turn-1"
        session.status = "RUNNING_TURN"
        store.save_session(session)

        class Notification:
            def __init__(self, method: str, params: dict):
                self.method = method
                self.params = params

        telegram = FakeTelegramClient()
        codex = FakeCodex()
        codex.pending_notifications.extend(
            [
                Notification("item/reasoning/textDelta", {"threadId": "thread-1", "turnId": "turn-1", "delta": "Checking "}),
                Notification("item/reasoning/textDelta", {"threadId": "thread-1", "turnId": "turn-1", "delta": "release notes"}),
            ]
        )

        drain_codex_notifications(self.paths, auth, telegram, self.recorder, codex)

        updated = store.get_or_create_telegram_session(auth)
        self.assertEqual(telegram.messages, [(22, "Checking release notes")])
        self.assertEqual(telegram.edits, [])
        self.assertEqual(updated.thinking_message_text, "Checking release notes")

    def test_drain_codex_notifications_reads_reasoning_arrays_from_completed_items(self) -> None:
        auth = AuthState(
            bot_token="token",
            telegram_user_id=11,
            telegram_chat_id=22,
            paired_at="now",
        )
        store = SessionStore(self.paths)
        session = store.get_or_create_telegram_session(auth)
        session.thread_id = "thread-1"
        session.active_turn_id = "turn-1"
        session.status = "RUNNING_TURN"
        store.save_session(session)

        class Notification:
            def __init__(self, method: str, params: dict):
                self.method = method
                self.params = params

        telegram = FakeTelegramClient()
        codex = FakeCodex()
        codex.pending_notifications.append(
            Notification(
                "item/completed",
                {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "item": {"type": "reasoning", "summary": ["Checking docs", "Comparing schemas"]},
                },
            )
        )

        drain_codex_notifications(self.paths, auth, telegram, self.recorder, codex)

        updated = store.get_or_create_telegram_session(auth)
        self.assertEqual(telegram.messages, [(22, "Checking docs\nComparing schemas")])
        self.assertEqual(updated.thinking_message_text, "Checking docs\nComparing schemas")

    def test_drain_codex_notifications_streams_short_item_agent_message_delta(self) -> None:
        auth = AuthState(
            bot_token="token",
            telegram_user_id=11,
            telegram_chat_id=22,
            paired_at="now",
        )
        store = SessionStore(self.paths)
        session = store.get_or_create_telegram_session(auth)
        session.thread_id = "thread-1"
        session.active_turn_id = "turn-1"
        session.status = "RUNNING_TURN"
        session.streaming_message_id = 1
        session.thinking_message_text = "Thinking..."
        store.save_session(session)

        class Notification:
            def __init__(self, method: str, params: dict):
                self.method = method
                self.params = params

        telegram = FakeTelegramClient()
        codex = FakeCodex()
        codex.pending_notifications.append(
            Notification("item/agentMessage/delta", {"threadId": "thread-1", "turnId": "turn-1", "delta": "Hello"})
        )

        drain_codex_notifications(self.paths, auth, telegram, self.recorder, codex)

        updated = store.get_or_create_telegram_session(auth)
        self.assertEqual(updated.streaming_output_text, "Hello")
        self.assertEqual(updated.pending_output_text, "")
        self.assertEqual(updated.thinking_message_text, "")
        self.assertEqual(telegram.edits, [(22, 1, "Hello")])

    def test_drain_codex_notifications_streams_commentary_agent_message_deltas(self) -> None:
        auth = AuthState(
            bot_token="token",
            telegram_user_id=11,
            telegram_chat_id=22,
            paired_at="now",
        )
        store = SessionStore(self.paths)
        session = store.get_or_create_telegram_session(auth)
        session.thread_id = "thread-1"
        session.active_turn_id = "turn-1"
        session.status = "RUNNING_TURN"
        session.streaming_message_id = 1
        session.thinking_message_text = "Thinking..."
        store.save_session(session)

        class Notification:
            def __init__(self, method: str, params: dict):
                self.method = method
                self.params = params

        telegram = FakeTelegramClient()
        codex = FakeCodex()
        codex.pending_notifications.extend(
            [
                Notification(
                    "item/started",
                    {
                        "threadId": "thread-1",
                        "turnId": "turn-1",
                        "item": {"id": "msg-1", "type": "agentMessage", "phase": "commentary", "text": ""},
                    },
                ),
                Notification(
                    "item/agentMessage/delta",
                    {"threadId": "thread-1", "turnId": "turn-1", "itemId": "msg-1", "delta": "I am using a skill."},
                ),
                Notification(
                    "item/completed",
                    {
                        "threadId": "thread-1",
                        "turnId": "turn-1",
                        "item": {"id": "msg-1", "type": "agentMessage", "phase": "commentary", "text": "I am using a skill."},
                    },
                ),
            ]
        )

        drain_codex_notifications(self.paths, auth, telegram, self.recorder, codex)

        updated = store.get_or_create_telegram_session(auth)
        self.assertEqual(updated.streaming_message_id, 1)
        self.assertEqual(updated.streaming_phase, "commentary")
        self.assertEqual(updated.thinking_message_text, "I am using a skill.")
        self.assertEqual(telegram.edits, [(22, 1, "I am using a skill.")])

    def test_drain_codex_notifications_replaces_commentary_with_final_answer(self) -> None:
        auth = AuthState(
            bot_token="token",
            telegram_user_id=11,
            telegram_chat_id=22,
            paired_at="now",
        )
        store = SessionStore(self.paths)
        session = store.get_or_create_telegram_session(auth)
        session.thread_id = "thread-1"
        session.active_turn_id = "turn-1"
        session.status = "RUNNING_TURN"
        session.streaming_message_id = 1
        session.thinking_message_text = "I am using a skill."
        session.streaming_phase = "commentary"
        store.save_session(session)

        class Notification:
            def __init__(self, method: str, params: dict):
                self.method = method
                self.params = params

        telegram = FakeTelegramClient()
        codex = FakeCodex()
        codex.pending_notifications.append(
            Notification("item/agentMessage/delta", {"threadId": "thread-1", "turnId": "turn-1", "delta": "Final answer"})
        )

        drain_codex_notifications(self.paths, auth, telegram, self.recorder, codex)

        updated = store.get_or_create_telegram_session(auth)
        self.assertEqual(updated.streaming_output_text, "Final answer")
        self.assertEqual(updated.pending_output_text, "")
        self.assertEqual(updated.streaming_phase, "answer")
        self.assertEqual(telegram.edits, [(22, 1, "Final answer")])

    def test_status_after_answer_is_appended_to_same_in_progress_message(self) -> None:
        auth = AuthState(
            bot_token="token",
            telegram_user_id=11,
            telegram_chat_id=22,
            paired_at="now",
        )
        store = SessionStore(self.paths)
        session = store.get_or_create_telegram_session(auth)
        session.thread_id = "thread-1"
        session.active_turn_id = "turn-1"
        session.status = "RUNNING_TURN"
        session.streaming_message_id = 1
        session.streaming_output_text = "Final answer"
        session.thinking_message_text = "Checking repo"
        session.streaming_phase = "answer"
        store.save_session(session)

        class Notification:
            def __init__(self, method: str, params: dict):
                self.method = method
                self.params = params

        telegram = FakeTelegramClient()
        codex = FakeCodex()
        codex.pending_notifications.extend(
            [
                Notification("item/agentMessage/delta", {"threadId": "thread-1", "turnId": "turn-1", "delta": "Final answer"}),
                Notification("thread/tokenUsage/updated", {"threadId": "thread-1", "turnId": "turn-1", "tokenUsage": {}}),
                Notification("turn/completed", {"turnId": "turn-1", "outputText": "Final answer"}),
            ]
        )

        drain_codex_notifications(self.paths, auth, telegram, self.recorder, codex)

        updated = store.get_or_create_telegram_session(auth)
        self.assertEqual(telegram.edits, [])
        self.assertEqual(telegram.messages, [])
        self.assertEqual(telegram.deletes, [(22, 1)])
        self.assertIsNone(updated.thinking_message_id)
        self.assertIsNone(updated.streaming_message_id)
        self.assertEqual(updated.last_delivered_output_text, "Final answer")

    def test_commentary_stream_keeps_only_latest_pending_update_until_min_interval(self) -> None:
        auth = AuthState(
            bot_token="token",
            telegram_user_id=11,
            telegram_chat_id=22,
            paired_at="now",
        )
        store = SessionStore(self.paths)
        session = store.get_or_create_telegram_session(auth)
        session.thread_id = "thread-1"
        session.active_turn_id = "turn-1"
        session.status = "RUNNING_TURN"
        session.streaming_message_id = 1
        session.thinking_message_text = "Thinking"
        session.streaming_phase = "commentary"
        session.last_agent_message_at = datetime.now(timezone.utc).isoformat()
        store.save_session(session)

        class Notification:
            def __init__(self, method: str, params: dict):
                self.method = method
                self.params = params

        telegram = FakeTelegramClient()
        codex = FakeCodex()
        codex.pending_notifications.extend(
            [
                Notification(
                    "item/started",
                    {
                        "threadId": "thread-1",
                        "turnId": "turn-1",
                        "item": {"id": "msg-1", "type": "agentMessage", "phase": "commentary", "text": ""},
                    },
                ),
                Notification(
                    "item/agentMessage/delta",
                    {"threadId": "thread-1", "turnId": "turn-1", "itemId": "msg-1", "delta": "Checking"},
                ),
                Notification(
                    "item/agentMessage/delta",
                    {"threadId": "thread-1", "turnId": "turn-1", "itemId": "msg-1", "delta": " repo"},
                ),
            ]
        )

        drain_codex_notifications(self.paths, auth, telegram, self.recorder, codex)

        updated = store.get_or_create_telegram_session(auth)
        self.assertEqual(updated.pending_output_text, "")
        self.assertEqual(updated.thinking_message_text, "Checking repo")
        self.assertEqual(telegram.edits, [])

        flush_idle_partial_outputs(
            self.paths,
            auth,
            telegram,
            self.recorder,
            store,
            idle_seconds=1.0,
            now=datetime.now(timezone.utc) + timedelta(seconds=2),
        )

        refreshed = store.get_or_create_telegram_session(auth)
        self.assertEqual(refreshed.streaming_message_id, 1)
        self.assertEqual(refreshed.thinking_message_text, "Checking repo")

    def test_thinking_edit_failure_does_not_block_final_group_reply(self) -> None:
        auth = AuthState(
            bot_token="token",
            telegram_user_id=11,
            telegram_chat_id=-1001,
            paired_at="now",
        )
        store = SessionStore(self.paths)
        session = store.get_or_create_telegram_session(auth, topic_id=120)
        session.thread_id = "thread-1"
        session.active_turn_id = "turn-1"
        session.status = "RUNNING_TURN"
        session.streaming_message_id = 7
        session.thinking_message_text = "Checking repo"
        session.streaming_phase = "commentary"
        store.save_session(session)

        class Notification:
            def __init__(self, method: str, params: dict):
                self.method = method
                self.params = params

        class FailingThinkingTelegram(FakeTelegramClient):
            def edit_message_text(self, chat_id: int, message_id: int, text: str, parse_mode: str | None = None) -> dict:
                if text.startswith("Thinking"):
                    raise TelegramError("HTTP Error 400: Bad Request")
                return super().edit_message_text(chat_id, message_id, text, parse_mode=parse_mode)

        telegram = FailingThinkingTelegram()
        codex = FakeCodex()
        codex.pending_notifications.extend(
            [
                Notification(
                    "item/started",
                    {
                        "threadId": "thread-1",
                        "turnId": "turn-1",
                        "item": {"id": "msg-1", "type": "agentMessage", "phase": "commentary", "text": ""},
                    },
                ),
                Notification(
                    "item/agentMessage/delta",
                    {"threadId": "thread-1", "turnId": "turn-1", "itemId": "msg-1", "delta": "Checking repo"},
                ),
                Notification(
                    "item/completed",
                    {
                        "threadId": "thread-1",
                        "turnId": "turn-1",
                        "item": {"id": "msg-2", "type": "agentMessage", "phase": "final_answer", "text": "Final answer"},
                    },
                ),
                Notification("turn/completed", {"turnId": "turn-1", "outputText": "Final answer"}),
            ]
        )

        drain_codex_notifications(self.paths, auth, telegram, self.recorder, codex)

        updated = store.get_or_create_telegram_session(auth, topic_id=120)
        self.assertEqual(updated.last_delivered_output_text, "Final answer")
        self.assertEqual(updated.streaming_message_id, None)
        self.assertEqual(
            telegram.edits,
            [
                (-1001, 7, "Checking repo"),
                (-1001, 7, "<blockquote expandable>Checking repo</blockquote>\n\nFinal answer"),
            ],
        )

    def test_drain_codex_notifications_respects_max_notifications_budget(self) -> None:
        auth = AuthState(
            bot_token="token",
            telegram_user_id=11,
            telegram_chat_id=22,
            paired_at="now",
        )

        class Notification:
            def __init__(self, method: str, params: dict):
                self.method = method
                self.params = params

        telegram = FakeTelegramClient()
        codex = FakeCodex()
        codex.pending_notifications.extend(
            [
                Notification("thread/tokenUsage/updated", {"threadId": "thread-1", "turnId": "turn-1"}),
                Notification("thread/tokenUsage/updated", {"threadId": "thread-1", "turnId": "turn-1"}),
                Notification("thread/tokenUsage/updated", {"threadId": "thread-1", "turnId": "turn-1"}),
            ]
        )

        handled = drain_codex_notifications(
            self.paths,
            auth,
            telegram,
            self.recorder,
            codex,
            max_notifications=2,
        )

        self.assertEqual(handled, 2)
        self.assertEqual(len(codex.pending_notifications), 1)

    def test_drain_codex_notifications_streams_item_agent_message_delta(self) -> None:
        auth = AuthState(
            bot_token="token",
            telegram_user_id=11,
            telegram_chat_id=22,
            paired_at="now",
        )
        store = SessionStore(self.paths)
        session = store.get_or_create_telegram_session(auth)
        session.thread_id = "thread-1"
        session.active_turn_id = "turn-1"
        session.status = "RUNNING_TURN"
        session.streaming_message_id = 1
        session.thinking_message_text = "Thinking..."
        store.save_session(session)

        class Notification:
            def __init__(self, method: str, params: dict):
                self.method = method
                self.params = params

        telegram = FakeTelegramClient()
        codex = FakeCodex()
        codex.pending_notifications.append(
            Notification(
                "item/agentMessage/delta",
                {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "delta": "Collecting release notes and grouping changes by date.",
                },
            )
        )

        drain_codex_notifications(self.paths, auth, telegram, self.recorder, codex)

        updated = store.get_or_create_telegram_session(auth)
        self.assertEqual(updated.thinking_message_text, "")
        self.assertEqual(updated.streaming_output_text, "Collecting release notes and grouping changes by date.")
        self.assertEqual(updated.pending_output_text, "")
        self.assertEqual(telegram.edits, [(22, 1, "Collecting release notes and grouping changes by date.")])

    def test_extract_assistant_text_reads_structured_text_object(self) -> None:
        text = extract_assistant_text(
            {
                "item": {
                    "type": "agentMessage",
                    "text": {"text": "Structured final answer"},
                }
            }
        )
        self.assertEqual(text, "Structured final answer")

    def test_extract_assistant_text_ignores_commentary_agent_item(self) -> None:
        text = extract_assistant_text(
            {
                "item": {
                    "type": "agentMessage",
                    "phase": "commentary",
                    "text": "I am browsing docs now.",
                }
            }
        )

        self.assertIsNone(text)

    def test_flush_idle_partial_outputs_flushes_after_idle_gap(self) -> None:
        auth = AuthState(
            bot_token="token",
            telegram_user_id=11,
            telegram_chat_id=22,
            paired_at="now",
        )
        store = SessionStore(self.paths)
        session = store.get_or_create_telegram_session(auth)
        session.pending_output_text = "Hello"
        session.pending_output_updated_at = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
        store.save_session(session)

        telegram = FakeTelegramClient()
        flush_idle_partial_outputs(
            self.paths,
            auth,
            telegram,
            self.recorder,
            store,
            idle_seconds=3.0,
            now=datetime.now(timezone.utc),
        )

        updated = store.get_or_create_telegram_session(auth)
        self.assertEqual(updated.pending_output_text, "")
        self.assertEqual(updated.last_delivered_output_text, "Hello")
        self.assertEqual(telegram.messages, [(22, "Hello")])
        self.assertEqual(self.recorder.records, [("assistant", "Hello")])

    def test_flush_idle_partial_outputs_is_suppressed_while_approval_is_pending(self) -> None:
        auth = AuthState(
            bot_token="token",
            telegram_user_id=11,
            telegram_chat_id=22,
            paired_at="now",
        )
        store = SessionStore(self.paths)
        session = store.get_or_create_telegram_session(auth)
        session.pending_output_text = "Hello"
        session.pending_output_updated_at = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
        store.save_session(session)
        ApprovalStore(self.paths).add(ApprovalRecord(17, "approval/request", {"tool": "shell"}))

        telegram = FakeTelegramClient()
        flush_idle_partial_outputs(
            self.paths,
            auth,
            telegram,
            self.recorder,
            store,
            idle_seconds=3.0,
            now=datetime.now(timezone.utc),
        )

        updated = store.get_or_create_telegram_session(auth)
        self.assertEqual(updated.pending_output_text, "Hello")
        self.assertEqual(updated.last_delivered_output_text, "")
        self.assertEqual(telegram.messages, [])
        self.assertEqual(self.recorder.records, [])

    def test_maybe_send_typing_indicator_for_attached_active_turn(self) -> None:
        auth = AuthState(
            bot_token="token",
            telegram_user_id=11,
            telegram_chat_id=22,
            paired_at="now",
        )
        store = SessionStore(self.paths)
        session = store.get_or_create_telegram_session(auth)
        session.thread_id = "thread-1"
        session.active_turn_id = "turn-1"
        session.status = "RUNNING_TURN"
        store.save_session(session)

        telegram = FakeTelegramClient()
        sent_at = maybe_send_typing_indicator(
            self.paths,
            auth,
            telegram,
            store,
            interval_seconds=4.0,
            last_sent_at=None,
            now=datetime.now(timezone.utc),
        )

        self.assertIsNotNone(sent_at)
        self.assertEqual(telegram.typing_actions, [22])

    def test_maybe_send_typing_indicator_routes_to_session_chat(self) -> None:
        auth = AuthState(
            bot_token="token",
            telegram_user_id=11,
            telegram_chat_id=22,
            paired_at="now",
        )
        store = SessionStore(self.paths)
        session = store.get_or_create_telegram_session(auth)
        session.transport_chat_id = 44
        session.transport_topic_id = 77
        session.thread_id = "thread-1"
        session.active_turn_id = "turn-1"
        session.status = "RUNNING_TURN"
        store.save_session(session)

        telegram = FakeTelegramClient()
        sent_at = maybe_send_typing_indicator(
            self.paths,
            auth,
            telegram,
            store,
            interval_seconds=4.0,
            last_sent_at=None,
            now=datetime.now(timezone.utc),
        )

        self.assertIsNotNone(sent_at)
        self.assertEqual(telegram.typing_actions, [(44, 77)])

    def test_maybe_send_typing_indicator_is_suppressed_while_approval_is_pending(self) -> None:
        auth = AuthState(
            bot_token="token",
            telegram_user_id=11,
            telegram_chat_id=22,
            paired_at="now",
        )
        store = SessionStore(self.paths)
        session = store.get_or_create_telegram_session(auth)
        session.active_turn_id = "turn-1"
        session.status = "RUNNING_TURN"
        store.save_session(session)
        ApprovalStore(self.paths).add(ApprovalRecord(17, "approval/request", {"tool": "shell"}))

        telegram = FakeTelegramClient()
        sent_at = maybe_send_typing_indicator(
            self.paths,
            auth,
            telegram,
            store,
            interval_seconds=4.0,
            last_sent_at=None,
            now=datetime.now(timezone.utc),
        )

        self.assertIsNone(sent_at)
        self.assertEqual(telegram.typing_actions, [])

    def test_maybe_send_typing_indicator_is_suppressed_for_stale_active_turn(self) -> None:
        auth = AuthState(
            bot_token="token",
            telegram_user_id=11,
            telegram_chat_id=22,
            paired_at="now",
        )
        store = SessionStore(self.paths)
        session = store.get_or_create_telegram_session(auth)
        session.active_turn_id = "turn-1"
        session.status = "RUNNING_TURN"
        session.last_user_message_at = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        store.save_session(session)

        telegram = FakeTelegramClient()
        sent_at = maybe_send_typing_indicator(
            self.paths,
            auth,
            telegram,
            store,
            interval_seconds=4.0,
            last_sent_at=None,
            now=datetime.now(timezone.utc),
        )

        self.assertIsNone(sent_at)
        self.assertEqual(telegram.typing_actions, [])


if __name__ == "__main__":
    unittest.main()
