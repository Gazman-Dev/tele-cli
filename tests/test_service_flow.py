from __future__ import annotations

from datetime import datetime, timedelta, timezone
import uuid
import unittest
from pathlib import Path
from unittest.mock import patch

from core.json_store import load_json
from core.models import AuthState, CodexServerState, Config, RuntimeState
from core.paths import build_paths
from core.state_versions import load_versioned_state
from runtime.approval_store import ApprovalRecord, ApprovalStore
from runtime.app_server_runtime import make_app_server_start_fn
from runtime.runtime import ServiceRuntime
from runtime.service import (
    bootstrap_paired_codex,
    drain_codex_approvals,
    drain_codex_notifications,
    flush_idle_partial_outputs,
    maybe_send_typing_indicator,
    process_telegram_update,
)
from runtime.session_store import SessionStore
from tests.fakes.fake_app_server import FakeAppServer, InMemoryJsonRpcTransport
from tests.fakes.fake_telegram import FakeTelegramClient


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
        self.assertEqual(len(telegram.messages), 1)
        self.assertIn("codex=DEGRADED", telegram.messages[0][1])
        self.assertIn("sessions=0", telegram.messages[0][1])
        self.assertEqual(self.recorder.records, [])

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
        self.assertIn("active_session_status=RECOVERING_TURN", telegram.messages[0][1])

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
        self.assertEqual(telegram.messages, [(22, "Tele Cli service connected to Codex App Server.")])

    def test_bootstrap_paired_codex_reports_auth_required_without_failing(self) -> None:
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
        self.assertEqual(
            telegram.messages,
            [
                (22, "Codex login is required. Telegram remains available."),
                (22, "Complete Codex login: https://example.test/login"),
            ],
        )

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
        recovery_log = self.paths.recovery_log.read_text(encoding="utf-8")
        self.assertIn("session_detached_on_new", recovery_log)
        self.assertIn("session_attached_on_new", recovery_log)

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
        from core.json_store import save_json

        save_json(self.paths.codex_server, persisted.to_dict())
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

        updated = load_versioned_state(self.paths.codex_server, CodexServerState.from_dict)
        self.assertIsNotNone(updated)
        assert updated is not None
        self.assertFalse(updated.auth_required)
        self.assertEqual(updated.account_status, "ready")
        self.assertEqual(updated.account_type, "chatgpt")
        self.assertIsNone(updated.login_url)
        self.assertIsNone(updated.login_type)
        self.assertEqual(self.runtime_state.codex_state, "RUNNING")
        self.assertEqual(telegram.messages, [(22, "Codex login completed. Telegram and Codex are ready.")])

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
        self.assertFalse(any(session.session_id == original.session_id for session in sessions))
        current = next(session for session in sessions if session.session_id == active.session_id)
        self.assertEqual(current.status, "ACTIVE")
        self.assertEqual(telegram.messages, [])
        self.assertEqual(self.recorder.records, [])
        recovery_log = self.paths.recovery_log.read_text(encoding="utf-8")
        self.assertIn("hidden_session_output_consumed", recovery_log)
        self.assertIn("detached_sessions_pruned count=1", recovery_log)

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


if __name__ == "__main__":
    unittest.main()
