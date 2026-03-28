from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core.models import AuthState, CodexServerState, Config, RuntimeState
from core.paths import build_paths
from core.state_versions import load_versioned_state
from runtime.instructions import build_instruction_paths
from runtime.app_server_runtime import (
    AppServerSession,
    bootstrap_app_server_session,
    build_app_server_command,
    derive_codex_state,
    make_app_server_start_fn,
    normalize_initialize_result,
    validate_initialize_result,
)
from runtime.runtime import ServiceRuntime
from tests.fakes.fake_app_server import FakeAppServer, InMemoryJsonRpcTransport
from tests.fakes.fake_telegram import FakeTelegramClient


class AppServerRuntimeTests(unittest.TestCase):
    def test_build_app_server_command_uses_stdio_mode(self) -> None:
        config = Config(state_dir="/repo", codex_command=["codex"])
        self.assertEqual(build_app_server_command(config), ["codex", "app-server", "--listen", "stdio://"])

    def test_derive_codex_state_maps_auth_required_states(self) -> None:
        self.assertEqual(derive_codex_state({"status": "auth_required"}), "AUTH_REQUIRED")
        self.assertEqual(derive_codex_state({"status": "expired"}), "AUTH_REQUIRED")
        self.assertEqual(derive_codex_state({"status": "ready"}), "RUNNING")
        self.assertEqual(
            derive_codex_state({"account": {"accountType": "chatgpt"}, "requiresOpenaiAuth": True}),
            "RUNNING",
        )

    def test_validate_initialize_result_requires_protocol_version_and_threads(self) -> None:
        with self.assertRaises(RuntimeError):
            validate_initialize_result({})
        with self.assertRaises(RuntimeError):
            validate_initialize_result({"protocolVersion": "1.0", "capabilities": {}})
        validate_initialize_result({"protocolVersion": "1.0", "capabilities": {"threads": True}})

    def test_validate_initialize_result_accepts_user_agent_only_response(self) -> None:
        normalized = normalize_initialize_result({"userAgent": "tele-cli/0.114.0"})

        validate_initialize_result(normalized)

        self.assertEqual(normalized["protocolVersion"], "user-agent-only")
        self.assertTrue(normalized["capabilities"]["threads"])

    def test_bootstrap_persists_codex_server_state_and_runtime_running(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            runtime_state = RuntimeState(
                session_id="1",
                service_state="RUNNING",
                codex_state="STOPPED",
                telegram_state="RUNNING",
                recorder_state="RUNNING",
                debug_state="RUNNING",
            )
            runtime = ServiceRuntime(runtime_state)
            transport = InMemoryJsonRpcTransport()
            server = FakeAppServer(transport)
            server.on("initialize", lambda payload: {"protocolVersion": "1.0", "capabilities": {"threads": True}})
            server.on("getAccount", lambda payload: {"status": "ready", "accountType": "chatgpt"})

            session = bootstrap_app_server_session(
                paths=paths,
                auth=AuthState(bot_token="token", telegram_user_id=11, telegram_chat_id=22, paired_at="now"),
                runtime=runtime,
                runtime_state=runtime_state,
                transport=transport,
                config=Config(state_dir=str(paths.root)),
            )

            self.assertIsInstance(session, AppServerSession)
            self.assertEqual(runtime_state.codex_state, "RUNNING")
            persisted = load_versioned_state(paths.codex_server, CodexServerState.from_dict)
            self.assertIsNotNone(persisted)
            assert persisted is not None
            self.assertTrue(persisted.initialized)
            self.assertEqual(persisted.protocol_version, "1.0")
            self.assertEqual(persisted.account_status, "ready")
            self.assertEqual(persisted.account_type, "chatgpt")

    def test_bootstrap_marks_auth_required_without_breaking_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            runtime_state = RuntimeState(
                session_id="1",
                service_state="RUNNING",
                codex_state="STOPPED",
                telegram_state="RUNNING",
                recorder_state="RUNNING",
                debug_state="RUNNING",
            )
            runtime = ServiceRuntime(runtime_state)
            transport = InMemoryJsonRpcTransport()
            server = FakeAppServer(transport)
            server.on("initialize", lambda payload: {"protocolVersion": "1.0", "capabilities": {"threads": True}})
            server.on("getAccount", lambda payload: {"status": "auth_required"})
            server.on("login/account", lambda payload: {"type": "chatgpt", "authUrl": "https://example.test/login"})

            bootstrap_app_server_session(
                paths=paths,
                auth=AuthState(bot_token="token", telegram_user_id=11, telegram_chat_id=22, paired_at="now"),
                runtime=runtime,
                runtime_state=runtime_state,
                transport=transport,
                config=Config(state_dir=str(paths.root)),
            )

            self.assertEqual(runtime_state.codex_state, "AUTH_REQUIRED")
            persisted = load_versioned_state(paths.codex_server, CodexServerState.from_dict)
            self.assertIsNotNone(persisted)
            assert persisted is not None
            self.assertTrue(persisted.auth_required)
            self.assertEqual(persisted.login_type, "chatgpt")
            self.assertEqual(persisted.login_url, "https://example.test/login")

    def test_app_server_session_send_steers_when_turn_is_already_active(self) -> None:
        transport = InMemoryJsonRpcTransport()
        server = FakeAppServer(transport)
        server.on("initialize", lambda payload: {"protocolVersion": "1.0", "capabilities": {"threads": True}})
        server.on("getAccount", lambda payload: {"status": "ready"})
        server.on("thread/start", lambda payload: {"threadId": "thread-1"})
        server.on("turn/start", lambda payload: {"turnId": "turn-1"})
        server.on("turn/steer", lambda payload: {"turnId": payload["params"]["turnId"]})

        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            runtime_state = RuntimeState(
                session_id="1",
                service_state="RUNNING",
                codex_state="STOPPED",
                telegram_state="RUNNING",
                recorder_state="RUNNING",
                debug_state="RUNNING",
            )
            runtime = ServiceRuntime(runtime_state)
            session = bootstrap_app_server_session(
                paths=paths,
                auth=AuthState(bot_token="token", telegram_user_id=11, telegram_chat_id=22, paired_at="now"),
                runtime=runtime,
                runtime_state=runtime_state,
                transport=transport,
                config=Config(state_dir=str(paths.root)),
            )
            session.send("hello")
            session.send("again")

        methods = [payload["method"] for payload in server.received]
        self.assertEqual(methods.count("thread/start"), 1)
        self.assertEqual(methods.count("turn/start"), 1)
        self.assertEqual(methods.count("turn/steer"), 1)
        thread_start = next(payload for payload in server.received if payload["method"] == "thread/start")
        self.assertEqual(thread_start["params"]["sandbox"], "danger-full-access")
        self.assertEqual(thread_start["params"]["approvalPolicy"], "never")
        self.assertEqual(thread_start["params"]["personality"], "pragmatic")
        self.assertEqual(thread_start["params"]["cwd"], str(build_instruction_paths(paths).repo_root))
        turn_start = next(payload for payload in server.received if payload["method"] == "turn/start")
        self.assertEqual(turn_start["params"]["approvalPolicy"], "never")
        self.assertEqual(turn_start["params"]["sandboxPolicy"], {"type": "dangerFullAccess"})
        self.assertEqual(turn_start["params"]["cwd"], str(build_instruction_paths(paths).repo_root))
        self.assertEqual(turn_start["params"]["personality"], "pragmatic")
        first_input = turn_start["params"]["input"][0]["text"]
        self.assertIn("You are Tele Cli, a Telegram-first personal assistant", first_input)
        self.assertIn("memory/sessions/", first_input)
        self.assertIn("User request:\nhello", first_input)
        turn_steer = next(payload for payload in server.received if payload["method"] == "turn/steer")
        self.assertEqual(turn_steer["params"]["turnId"], "turn-1")
        self.assertEqual(turn_steer["params"]["input"], [{"type": "text", "text": "again"}])

    def test_app_server_session_send_interrupts_stale_active_turn_before_new_turn(self) -> None:
        transport = InMemoryJsonRpcTransport()
        server = FakeAppServer(transport)
        server.on("initialize", lambda payload: {"protocolVersion": "1.0", "capabilities": {"threads": True}})
        server.on("getAccount", lambda payload: {"status": "ready"})
        server.on("thread/start", lambda payload: {"threadId": "thread-1"})
        turn_counter = {"count": 0}

        def handle_turn_start(payload):
            turn_counter["count"] += 1
            return {"turnId": f"turn-{turn_counter['count']}"}

        server.on("turn/start", handle_turn_start)
        server.on("turn/interrupt", lambda payload: {"ok": True})
        server.on("turn/steer", lambda payload: {"turnId": payload["params"]["turnId"]})

        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            auth = AuthState(bot_token="token", telegram_user_id=11, telegram_chat_id=22, paired_at="now")
            runtime_state = RuntimeState(
                session_id="1",
                service_state="RUNNING",
                codex_state="STOPPED",
                telegram_state="RUNNING",
                recorder_state="RUNNING",
                debug_state="RUNNING",
            )
            runtime = ServiceRuntime(runtime_state)
            session = bootstrap_app_server_session(
                paths=paths,
                auth=auth,
                runtime=runtime,
                runtime_state=runtime_state,
                transport=transport,
                config=Config(state_dir=str(paths.root)),
            )
            recovered = session.send("hello")
            self.assertFalse(recovered)

            from runtime.session_store import SessionStore

            store = SessionStore(paths)
            current = store.get_current_telegram_session(auth)
            assert current is not None
            current.last_agent_message_at = (datetime.now(timezone.utc) - timedelta(seconds=45)).isoformat()
            current.pending_output_updated_at = current.last_agent_message_at
            current.streaming_message_id = 99
            current.streaming_output_text = "stale partial"
            current.pending_output_text = "stale pending"
            current.thinking_message_text = "Thinking..."
            store.save_session(current)

            recovered = session.send("again")
            self.assertTrue(recovered)

            refreshed = store.get_current_telegram_session(auth)
            assert refreshed is not None
            self.assertEqual(refreshed.active_turn_id, "turn-2")
            self.assertEqual(refreshed.pending_output_text, "")
            self.assertEqual(refreshed.streaming_output_text, "")
            self.assertEqual(refreshed.thinking_message_text, "")

        methods = [payload["method"] for payload in server.received]
        self.assertEqual(methods.count("turn/start"), 2)
        self.assertEqual(methods.count("turn/interrupt"), 1)
        self.assertEqual(methods.count("turn/steer"), 0)
        turn_starts = [payload for payload in server.received if payload["method"] == "turn/start"]
        second_input = turn_starts[-1]["params"]["input"][0]["text"]
        self.assertIn("System: recovered from error, the previous message got interrupted.", second_input)
        self.assertIn("---", second_input)
        self.assertIn("again", second_input)

    def test_send_starts_new_thread_when_resume_rejects_stale_thread(self) -> None:
        transport = InMemoryJsonRpcTransport()
        server = FakeAppServer(transport)
        server.on("initialize", lambda payload: {"protocolVersion": "1.0", "capabilities": {"threads": True}})
        server.on("getAccount", lambda payload: {"status": "ready"})
        server.on("thread/resume", lambda payload: (_ for _ in ()).throw(RuntimeError("no rollout found")))
        server.on("thread/start", lambda payload: {"threadId": "thread-2"})
        server.on("turn/start", lambda payload: {"turn": {"id": "turn-2"}})

        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            auth = AuthState(bot_token="token", telegram_user_id=11, telegram_chat_id=22, paired_at="now")
            from runtime.session_store import SessionStore

            store = SessionStore(paths)
            existing = store.get_or_create_telegram_session(auth)
            existing.thread_id = "thread-stale"
            existing.status = "ACTIVE"
            store.save_session(existing)

            runtime_state = RuntimeState(
                session_id="1",
                service_state="RUNNING",
                codex_state="STOPPED",
                telegram_state="RUNNING",
                recorder_state="RUNNING",
                debug_state="RUNNING",
            )
            runtime = ServiceRuntime(runtime_state)
            session = bootstrap_app_server_session(
                paths=paths,
                auth=auth,
                runtime=runtime,
                runtime_state=runtime_state,
                transport=transport,
                config=Config(state_dir=str(paths.root)),
            )

            session.send("hello")

            refreshed = store.get_current_telegram_session(auth)
            self.assertIsNotNone(refreshed)
            assert refreshed is not None
            self.assertEqual(refreshed.thread_id, "thread-2")
            self.assertEqual(refreshed.active_turn_id, "turn-2")

        methods = [payload["method"] for payload in server.received]
        self.assertIn("thread/resume", methods)
        self.assertIn("thread/start", methods)

    def test_interrupt_clears_active_turn_and_marks_session_interrupted(self) -> None:
        transport = InMemoryJsonRpcTransport()
        server = FakeAppServer(transport)
        server.on("initialize", lambda payload: {"protocolVersion": "1.0", "capabilities": {"threads": True}})
        server.on("getAccount", lambda payload: {"status": "ready"})
        server.on("thread/start", lambda payload: {"threadId": "thread-1"})
        server.on("turn/start", lambda payload: {"turnId": "turn-1"})
        server.on("turn/interrupt", lambda payload: {"ok": True})

        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            auth = AuthState(bot_token="token", telegram_user_id=11, telegram_chat_id=22, paired_at="now")
            runtime_state = RuntimeState(
                session_id="1",
                service_state="RUNNING",
                codex_state="STOPPED",
                telegram_state="RUNNING",
                recorder_state="RUNNING",
                debug_state="RUNNING",
            )
            runtime = ServiceRuntime(runtime_state)
            session = bootstrap_app_server_session(
                paths=paths,
                auth=auth,
                runtime=runtime,
                runtime_state=runtime_state,
                transport=transport,
                config=Config(state_dir=str(paths.root)),
            )
            session.send("hello")
            interrupted = session.interrupt()

            self.assertTrue(interrupted)
            methods = [payload["method"] for payload in server.received]
            self.assertIn("turn/interrupt", methods)

            from runtime.session_store import SessionStore

            stored_sessions = SessionStore(paths).list_telegram_sessions(auth)
            self.assertEqual(len(stored_sessions), 1)
            self.assertIsNone(stored_sessions[0].active_turn_id)
            self.assertEqual(stored_sessions[0].status, "INTERRUPTED")

    def test_app_server_session_send_local_uses_named_session(self) -> None:
        transport = InMemoryJsonRpcTransport()
        server = FakeAppServer(transport)
        server.on("initialize", lambda payload: {"protocolVersion": "1.0", "capabilities": {"threads": True}})
        server.on("getAccount", lambda payload: {"status": "ready"})
        server.on("thread/start", lambda payload: {"threadId": "thread-local-1"})
        server.on("turn/start", lambda payload: {"turnId": "turn-local-1"})

        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            runtime_state = RuntimeState(
                session_id="1",
                service_state="RUNNING",
                codex_state="STOPPED",
                telegram_state="RUNNING",
                recorder_state="RUNNING",
                debug_state="RUNNING",
            )
            runtime = ServiceRuntime(runtime_state)
            session = bootstrap_app_server_session(
                paths=paths,
                auth=AuthState(bot_token="token", telegram_user_id=11, telegram_chat_id=22, paired_at="now"),
                runtime=runtime,
                runtime_state=runtime_state,
                transport=transport,
                config=Config(state_dir=str(paths.root)),
            )
            session.send_local("my_group/topic1", "hello local")

            from runtime.session_store import SessionStore

            stored = SessionStore(paths).get_current_local_session("my_group/topic1")
            self.assertIsNotNone(stored)
            assert stored is not None
            self.assertEqual(stored.transport_channel, "my_group/topic1")
            self.assertEqual(stored.thread_id, "thread-local-1")
            self.assertEqual(stored.active_turn_id, "turn-local-1")

        turn_start = next(payload for payload in server.received if payload["method"] == "turn/start")
        first_input = turn_start["params"]["input"][0]["text"]
        self.assertIn("Current session name: my_group/topic1", first_input)
        self.assertIn("User request:\nhello local", first_input)

    def test_bootstrap_reuses_persisted_thread_id_via_resume(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            auth = AuthState(bot_token="token", telegram_user_id=11, telegram_chat_id=22, paired_at="now")

            transport1 = InMemoryJsonRpcTransport()
            server1 = FakeAppServer(transport1)
            server1.on("initialize", lambda payload: {"protocolVersion": "1.0", "capabilities": {"threads": True}})
            server1.on("getAccount", lambda payload: {"status": "ready"})
            server1.on("thread/start", lambda payload: {"threadId": "thread-1"})
            server1.on("turn/start", lambda payload: {"turnId": "turn-1"})

            runtime_state1 = RuntimeState(
                session_id="1",
                service_state="RUNNING",
                codex_state="STOPPED",
                telegram_state="RUNNING",
                recorder_state="RUNNING",
                debug_state="RUNNING",
            )
            runtime1 = ServiceRuntime(runtime_state1)
            session1 = bootstrap_app_server_session(
                paths=paths,
                auth=auth,
                runtime=runtime1,
                runtime_state=runtime_state1,
                transport=transport1,
                config=Config(state_dir=str(paths.root)),
            )
            session1.send("hello")
            session1.poll_notification()  # no-op if queue is empty
            from runtime.session_store import SessionStore

            store = SessionStore(paths)
            first_session = store.get_or_create_telegram_session(auth)
            first_session.active_turn_id = None
            first_session.status = "ACTIVE"
            store.save_session(first_session)
            session1.stop()

            transport2 = InMemoryJsonRpcTransport()
            server2 = FakeAppServer(transport2)
            server2.on("initialize", lambda payload: {"protocolVersion": "1.0", "capabilities": {"threads": True}})
            server2.on("getAccount", lambda payload: {"status": "ready"})
            server2.on("thread/resume", lambda payload: {"threadId": payload["params"]["threadId"]})
            server2.on("turn/start", lambda payload: {"turnId": "turn-2"})

            runtime_state2 = RuntimeState(
                session_id="2",
                service_state="RUNNING",
                codex_state="STOPPED",
                telegram_state="RUNNING",
                recorder_state="RUNNING",
                debug_state="RUNNING",
            )
            runtime2 = ServiceRuntime(runtime_state2)
            session2 = bootstrap_app_server_session(
                paths=paths,
                auth=auth,
                runtime=runtime2,
                runtime_state=runtime_state2,
                transport=transport2,
                config=Config(state_dir=str(paths.root)),
            )
            session2.send("again")

            methods = [payload["method"] for payload in server2.received]
            self.assertIn("thread/resume", methods)
            self.assertNotIn("thread/start", methods)
            self.assertIn("turn/start", methods)

    def test_bootstrap_recovers_inflight_turn_via_thread_resume(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            auth = AuthState(bot_token="token", telegram_user_id=11, telegram_chat_id=22, paired_at="now")

            from runtime.session_store import SessionStore

            store = SessionStore(paths)
            session = store.get_or_create_telegram_session(auth)
            session.thread_id = "thread-1"
            session.active_turn_id = "turn-1"
            session.status = "RUNNING_TURN"
            store.save_session(session)

            transport = InMemoryJsonRpcTransport()
            server = FakeAppServer(transport)
            server.on("initialize", lambda payload: {"protocolVersion": "1.0", "capabilities": {"threads": True}})
            server.on("getAccount", lambda payload: {"status": "ready"})
            server.on("thread/resume", lambda payload: {"threadId": payload["params"]["threadId"]})

            runtime_state = RuntimeState(
                session_id="1",
                service_state="RUNNING",
                codex_state="STOPPED",
                telegram_state="RUNNING",
                recorder_state="RUNNING",
                debug_state="RUNNING",
            )
            runtime = ServiceRuntime(runtime_state)
            bootstrap_app_server_session(
                paths=paths,
                auth=auth,
                runtime=runtime,
                runtime_state=runtime_state,
                transport=transport,
                config=Config(state_dir=str(paths.root)),
            )

            recovered = store.get_current_telegram_session(auth)
            self.assertIsNotNone(recovered)
            assert recovered is not None
            self.assertEqual(recovered.status, "RUNNING_TURN")
            methods = [payload["method"] for payload in server.received]
            self.assertIn("thread/resume", methods)

    def test_bootstrap_leaves_inflight_turn_recovering_when_resume_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            auth = AuthState(bot_token="token", telegram_user_id=11, telegram_chat_id=22, paired_at="now")

            from runtime.session_store import SessionStore

            store = SessionStore(paths)
            session = store.get_or_create_telegram_session(auth)
            session.thread_id = "thread-1"
            session.active_turn_id = "turn-1"
            session.status = "RUNNING_TURN"
            store.save_session(session)

            transport = InMemoryJsonRpcTransport()
            server = FakeAppServer(transport)
            server.on("initialize", lambda payload: {"protocolVersion": "1.0", "capabilities": {"threads": True}})
            server.on("getAccount", lambda payload: {"status": "ready"})
            server.on("thread/resume", lambda payload: (_ for _ in ()).throw(RuntimeError("resume failed")))

            runtime_state = RuntimeState(
                session_id="1",
                service_state="RUNNING",
                codex_state="STOPPED",
                telegram_state="RUNNING",
                recorder_state="RUNNING",
                debug_state="RUNNING",
            )
            runtime = ServiceRuntime(runtime_state)
            bootstrap_app_server_session(
                paths=paths,
                auth=auth,
                runtime=runtime,
                runtime_state=runtime_state,
                transport=transport,
                config=Config(state_dir=str(paths.root)),
            )

            recovered = store.get_current_telegram_session(auth)
            self.assertIsNotNone(recovered)
            assert recovered is not None
            self.assertEqual(recovered.status, "RECOVERING_TURN")

    def test_start_fn_keeps_telegram_quiet_when_recovering_turn_remains_after_boot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            auth = AuthState(bot_token="token", telegram_user_id=11, telegram_chat_id=22, paired_at="now")

            from runtime.session_store import SessionStore

            store = SessionStore(paths)
            session_record = store.get_or_create_telegram_session(auth)
            session_record.thread_id = "thread-1"
            session_record.active_turn_id = "turn-1"
            session_record.status = "RUNNING_TURN"
            store.save_session(session_record)

            transport = InMemoryJsonRpcTransport()
            server = FakeAppServer(transport)
            server.on("initialize", lambda payload: {"protocolVersion": "1.0", "capabilities": {"threads": True}})
            server.on("getAccount", lambda payload: {"status": "ready"})
            server.on("thread/resume", lambda payload: (_ for _ in ()).throw(RuntimeError("resume failed")))

            runtime_state = RuntimeState(
                session_id="1",
                service_state="RUNNING",
                codex_state="STOPPED",
                telegram_state="RUNNING",
                recorder_state="RUNNING",
                debug_state="RUNNING",
            )
            runtime = ServiceRuntime(runtime_state)
            telegram = FakeTelegramClient()
            start_fn = make_app_server_start_fn(paths, lambda config, auth: transport)

            session = start_fn(
                Config(state_dir=str(paths.root)),
                auth,
                runtime,
                runtime_state,
                object(),
                object(),
                telegram,
                lambda source, line: None,
            )

            self.assertIsNotNone(session)
            self.assertEqual(telegram.messages, [])

    def test_start_fn_does_not_send_recovery_notice_on_clean_boot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            runtime_state = RuntimeState(
                session_id="1",
                service_state="RUNNING",
                codex_state="STOPPED",
                telegram_state="RUNNING",
                recorder_state="RUNNING",
                debug_state="RUNNING",
            )
            runtime = ServiceRuntime(runtime_state)
            auth = AuthState(bot_token="token", telegram_user_id=11, telegram_chat_id=22, paired_at="now")
            telegram = FakeTelegramClient()
            transport = InMemoryJsonRpcTransport()
            server = FakeAppServer(transport)
            server.on("initialize", lambda payload: {"protocolVersion": "1.0", "capabilities": {"threads": True}})
            server.on("getAccount", lambda payload: {"status": "ready"})
            start_fn = make_app_server_start_fn(paths, lambda config, auth: transport)

            session = start_fn(
                Config(state_dir=str(paths.root)),
                auth,
                runtime,
                runtime_state,
                object(),
                object(),
                telegram,
                lambda source, line: None,
            )

            self.assertIsNotNone(session)
            self.assertEqual(telegram.messages, [])

    def test_start_fn_keeps_telegram_quiet_when_auth_is_required(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            runtime_state = RuntimeState(
                session_id="1",
                service_state="RUNNING",
                codex_state="STOPPED",
                telegram_state="RUNNING",
                recorder_state="RUNNING",
                debug_state="RUNNING",
            )
            runtime = ServiceRuntime(runtime_state)
            auth = AuthState(bot_token="token", telegram_user_id=11, telegram_chat_id=22, paired_at="now")
            telegram = FakeTelegramClient()
            transport = InMemoryJsonRpcTransport()
            server = FakeAppServer(transport)
            server.on("initialize", lambda payload: {"protocolVersion": "1.0", "capabilities": {"threads": True}})
            server.on("getAccount", lambda payload: {"status": "auth_required"})
            server.on("login/account", lambda payload: {"type": "chatgpt", "authUrl": "https://example.test/login"})
            start_fn = make_app_server_start_fn(paths, lambda config, auth: transport)

            session = start_fn(
                Config(state_dir=str(paths.root)),
                auth,
                runtime,
                runtime_state,
                object(),
                object(),
                telegram,
                lambda source, line: None,
            )

            self.assertIsNotNone(session)
            self.assertEqual(telegram.messages, [])

    def test_start_fn_keeps_running_when_success_notification_fails(self) -> None:
        class BrokenTelegramClient(FakeTelegramClient):
            def send_message(self, chat_id: int, text: str) -> None:
                raise TimeoutError("telegram timed out")

        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            runtime_state = RuntimeState(
                session_id="1",
                service_state="RUNNING",
                codex_state="STOPPED",
                telegram_state="RUNNING",
                recorder_state="RUNNING",
                debug_state="RUNNING",
            )
            runtime = ServiceRuntime(runtime_state)
            auth = AuthState(bot_token="token", telegram_user_id=11, telegram_chat_id=22, paired_at="now")
            telegram = BrokenTelegramClient()
            output: list[tuple[str, str]] = []
            transport = InMemoryJsonRpcTransport()
            server = FakeAppServer(transport)
            server.on("initialize", lambda payload: {"protocolVersion": "1.0", "capabilities": {"threads": True}})
            server.on("getAccount", lambda payload: {"status": "ready"})
            start_fn = make_app_server_start_fn(paths, lambda config, auth: transport)

            session = start_fn(
                Config(state_dir=str(paths.root)),
                auth,
                runtime,
                runtime_state,
                object(),
                object(),
                telegram,
                lambda source, line: output.append((source, line)),
            )

            self.assertIsNotNone(session)
            self.assertEqual(runtime_state.codex_state, "RUNNING")
            persisted = load_versioned_state(paths.codex_server, CodexServerState.from_dict)
            self.assertIsNotNone(persisted)
            assert persisted is not None
            self.assertTrue(persisted.initialized)
            self.assertEqual(output, [])

    def test_start_fn_returns_none_when_bootstrap_fails_and_failure_notification_fails(self) -> None:
        class BrokenTelegramClient(FakeTelegramClient):
            def send_message(self, chat_id: int, text: str) -> None:
                raise TimeoutError("telegram timed out")

        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            runtime_state = RuntimeState(
                session_id="1",
                service_state="RUNNING",
                codex_state="STOPPED",
                telegram_state="RUNNING",
                recorder_state="RUNNING",
                debug_state="RUNNING",
            )
            runtime = ServiceRuntime(runtime_state)
            auth = AuthState(bot_token="token", telegram_user_id=11, telegram_chat_id=22, paired_at="now")
            telegram = BrokenTelegramClient()
            output: list[tuple[str, str]] = []
            start_fn = make_app_server_start_fn(paths, lambda config, auth: (_ for _ in ()).throw(RuntimeError("boom")))

            session = start_fn(
                Config(state_dir=str(paths.root)),
                auth,
                runtime,
                runtime_state,
                object(),
                object(),
                telegram,
                lambda source, line: output.append((source, line)),
            )

            self.assertIsNone(session)
            self.assertEqual(runtime_state.codex_state, "DEGRADED")
            persisted = load_versioned_state(paths.codex_server, CodexServerState.from_dict)
            self.assertIsNotNone(persisted)
            assert persisted is not None
            self.assertFalse(persisted.initialized)
            self.assertEqual(persisted.last_error, "boom")
            self.assertEqual(output, [])

    def test_start_fn_keeps_telegram_quiet_for_repeated_failures(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            runtime_state = RuntimeState(
                session_id="1",
                service_state="RUNNING",
                codex_state="STOPPED",
                telegram_state="RUNNING",
                recorder_state="RUNNING",
                debug_state="RUNNING",
            )
            runtime = ServiceRuntime(runtime_state)
            auth = AuthState(bot_token="token", telegram_user_id=11, telegram_chat_id=22, paired_at="now")
            telegram = FakeTelegramClient()
            start_fn = make_app_server_start_fn(paths, lambda config, auth: (_ for _ in ()).throw(RuntimeError("boom")))

            first = start_fn(
                Config(state_dir=str(paths.root)),
                auth,
                runtime,
                runtime_state,
                object(),
                object(),
                telegram,
                lambda source, line: None,
            )
            second = start_fn(
                Config(state_dir=str(paths.root)),
                auth,
                runtime,
                runtime_state,
                object(),
                object(),
                telegram,
                lambda source, line: None,
            )

            self.assertIsNone(first)
            self.assertIsNone(second)
            self.assertEqual(telegram.messages, [])

    def test_start_fn_degrades_runtime_when_transport_factory_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            runtime_state = RuntimeState(
                session_id="1",
                service_state="RUNNING",
                codex_state="STOPPED",
                telegram_state="RUNNING",
                recorder_state="RUNNING",
                debug_state="RUNNING",
            )
            runtime = ServiceRuntime(runtime_state)
            auth = AuthState(bot_token="token", telegram_user_id=11, telegram_chat_id=22, paired_at="now")
            telegram = FakeTelegramClient()
            start_fn = make_app_server_start_fn(paths, lambda config, auth: (_ for _ in ()).throw(RuntimeError("boom")))

            session = start_fn(
                Config(state_dir=str(paths.root)),
                auth,
                runtime,
                runtime_state,
                object(),
                object(),
                telegram,
                lambda source, line: None,
            )

            self.assertIsNone(session)
            self.assertEqual(runtime_state.codex_state, "DEGRADED")
            persisted = load_versioned_state(paths.codex_server, CodexServerState.from_dict)
            self.assertIsNotNone(persisted)
            assert persisted is not None
            self.assertFalse(persisted.initialized)
            self.assertEqual(persisted.last_error, "boom")
            self.assertEqual(telegram.messages, [])

    def test_start_fn_degrades_runtime_when_initialize_is_incompatible(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            runtime_state = RuntimeState(
                session_id="1",
                service_state="RUNNING",
                codex_state="STOPPED",
                telegram_state="RUNNING",
                recorder_state="RUNNING",
                debug_state="RUNNING",
            )
            runtime = ServiceRuntime(runtime_state)
            auth = AuthState(bot_token="token", telegram_user_id=11, telegram_chat_id=22, paired_at="now")
            telegram = FakeTelegramClient()
            transport = InMemoryJsonRpcTransport()
            server = FakeAppServer(transport)
            server.on("initialize", lambda payload: {"protocolVersion": "1.0", "capabilities": {}})
            start_fn = make_app_server_start_fn(paths, lambda config, auth: transport)

            session = start_fn(
                Config(state_dir=str(paths.root)),
                auth,
                runtime,
                runtime_state,
                object(),
                object(),
                telegram,
                lambda source, line: None,
            )

            self.assertIsNone(session)
            self.assertEqual(runtime_state.codex_state, "DEGRADED")
            persisted = load_versioned_state(paths.codex_server, CodexServerState.from_dict)
            self.assertIsNotNone(persisted)
            assert persisted is not None
            self.assertIn("thread lifecycle", persisted.last_error)
            self.assertEqual(telegram.messages, [])

    def test_poll_approval_request_and_reply(self) -> None:
        transport = InMemoryJsonRpcTransport()
        server = FakeAppServer(transport)
        server.on("initialize", lambda payload: {"protocolVersion": "1.0", "capabilities": {"threads": True}})
        server.on("getAccount", lambda payload: {"status": "ready"})

        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            auth = AuthState(bot_token="token", telegram_user_id=11, telegram_chat_id=22, paired_at="now")
            runtime_state = RuntimeState(
                session_id="1",
                service_state="RUNNING",
                codex_state="STOPPED",
                telegram_state="RUNNING",
                recorder_state="RUNNING",
                debug_state="RUNNING",
            )
            runtime = ServiceRuntime(runtime_state)
            session = bootstrap_app_server_session(
                paths=paths,
                auth=auth,
                runtime=runtime,
                runtime_state=runtime_state,
                transport=transport,
                config=Config(state_dir=str(paths.root)),
            )
            server.request(88, "approval/request", {"tool": "shell", "command": "rm -rf /tmp"})
            approval = None
            for _ in range(20):
                approval = session.poll_approval_request()
                if approval is not None:
                    break
                import time

                time.sleep(0.01)
            self.assertIsNotNone(approval)
            assert approval is not None
            self.assertEqual(approval.request_id, 88)
            session.approve(88)

            self.assertEqual(server.responses[-1]["id"], 88)
            self.assertEqual(server.responses[-1]["result"], {"approved": True})

    def test_poll_notification_returns_server_notification(self) -> None:
        transport = InMemoryJsonRpcTransport()
        server = FakeAppServer(transport)
        server.on("initialize", lambda payload: {"protocolVersion": "1.0", "capabilities": {"threads": True}})
        server.on("getAccount", lambda payload: {"status": "ready"})

        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            auth = AuthState(bot_token="token", telegram_user_id=11, telegram_chat_id=22, paired_at="now")
            runtime_state = RuntimeState(
                session_id="1",
                service_state="RUNNING",
                codex_state="STOPPED",
                telegram_state="RUNNING",
                recorder_state="RUNNING",
                debug_state="RUNNING",
            )
            runtime = ServiceRuntime(runtime_state)
            session = bootstrap_app_server_session(
                paths=paths,
                auth=auth,
                runtime=runtime,
                runtime_state=runtime_state,
                transport=transport,
                config=Config(state_dir=str(paths.root)),
            )
            server.notify("turn/completed", {"turnId": "turn-1"})
            notification = None
            for _ in range(20):
                notification = session.poll_notification()
                if notification is not None:
                    break
                import time

                time.sleep(0.01)
            self.assertIsNotNone(notification)
            assert notification is not None
            self.assertEqual(notification.method, "turn/completed")


if __name__ == "__main__":
    unittest.main()
