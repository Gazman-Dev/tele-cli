from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.json_store import load_json, save_json
from core.models import AuthState, Config, RuntimeState
from core.paths import build_paths
from integrations.telegram import TelegramError
from runtime.approval_store import ApprovalRecord, ApprovalStore
from runtime.app_server_runtime import make_app_server_start_fn
from runtime.runtime import ServiceRuntime
from runtime.service import maintain_codex_runtime, run_service
from runtime.session_store import SessionStore
from runtime.telegram_update_store import TelegramUpdateStore
from storage.runtime_state_store import load_runtime_state
from tests.fakes.fake_app_server import FakeAppServer, InMemoryJsonRpcTransport


def load_event_types(paths) -> list[str]:
    with closing(sqlite3.connect(paths.database)) as connection:
        rows = connection.execute("SELECT event_type FROM events ORDER BY event_id").fetchall()
    return [str(row[0]) for row in rows]


def load_recovery_messages(paths) -> list[str]:
    with closing(sqlite3.connect(paths.database)) as connection:
        rows = connection.execute(
            "SELECT payload_json FROM events WHERE source = 'service' AND event_type = 'service.recovery' ORDER BY event_id"
        ).fetchall()
    return [json.loads(str(row[0])).get("message", "") for row in rows]


class FakeAppLock:
    def __init__(self) -> None:
        self.cleared = False

    def clear(self) -> None:
        self.cleared = True


class RestartingFakeCodex:
    def __init__(self, alive: bool) -> None:
        self.alive = alive
        self.stopped = False

    def is_alive(self) -> bool:
        return self.alive

    def stop(self) -> None:
        self.stopped = True


class SequentialTelegramClient:
    def __init__(self, batches: list[list[dict]], on_batch: dict[int, callable] | None = None) -> None:
        self._batches = list(batches)
        self._calls = 0
        self._on_batch = on_batch or {}
        self.messages: list[tuple[int, str]] = []
        self.edits: list[tuple[int, int, str]] = []
        self.typing_actions: list[int] = []

    def get_updates(self, offset=None, timeout: int = 20) -> list[dict]:
        self._calls += 1
        callback = self._on_batch.get(self._calls)
        if callback is not None:
            callback()
        if self._batches:
            return self._batches.pop(0)
        return []

    def send_message(self, chat_id: int, text: str) -> dict:
        self.messages.append((chat_id, text))
        return {"message_id": len(self.messages)}

    def edit_message_text(self, chat_id: int, message_id: int, text: str) -> dict:
        self.edits.append((chat_id, message_id, text))
        return {"message_id": message_id}

    def send_typing(self, chat_id: int) -> None:
        self.typing_actions.append(chat_id)


class FlakyTelegramClient(SequentialTelegramClient):
    def __init__(self, outcomes: list[object]) -> None:
        super().__init__(batches=[])
        self._outcomes = list(outcomes)

    def get_updates(self, offset=None, timeout: int = 20) -> list[dict]:
        self._calls += 1
        if self._outcomes:
            outcome = self._outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return list(outcome)
        return []


class CrashingTelegramClient(SequentialTelegramClient):
    def __init__(self, outcomes: list[object]) -> None:
        super().__init__(batches=[])
        self._outcomes = list(outcomes)

    def get_updates(self, offset=None, timeout: int = 20) -> list[dict]:
        self._calls += 1
        if self._outcomes:
            outcome = self._outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return list(outcome)
        return []


class ServiceLoopTests(unittest.TestCase):
    def _run_service_once(self, paths, telegram, start_fn, app_lock, sleep_side_effect=KeyboardInterrupt()) -> None:
        with (
            patch("runtime.service.TelegramClient", return_value=telegram),
            patch("runtime.service.prepare_service_lock", return_value=(app_lock, object())),
            patch("runtime.service.time.sleep", side_effect=sleep_side_effect),
        ):
            with self.assertRaises(KeyboardInterrupt):
                run_service(paths, start_codex_session_fn=start_fn)

    def test_run_service_hides_detached_completion_after_new(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            save_json(paths.config, Config(state_dir=str(paths.root)).to_dict())
            save_json(
                paths.auth,
                AuthState(bot_token="token", telegram_user_id=11, telegram_chat_id=22, paired_at="now").to_dict(),
            )

            transport = InMemoryJsonRpcTransport()
            server = FakeAppServer(transport)
            server.on("initialize", lambda payload: {"protocolVersion": "1.0", "capabilities": {"threads": True}})
            server.on("getAccount", lambda payload: {"status": "ready", "accountType": "chatgpt"})
            server.on("thread/start", lambda payload: {"threadId": "thread-1"})
            server.on("turn/start", lambda payload: {"turnId": "turn-1"})
            start_fn = make_app_server_start_fn(paths, lambda config, auth: transport)

            telegram = SequentialTelegramClient(
                batches=[
                    [{"update_id": 1, "message": {"chat": {"id": 22}, "from": {"id": 11}, "text": "hello"}}],
                    [{"update_id": 2, "message": {"chat": {"id": 22}, "from": {"id": 11}, "text": "/new"}}],
                ],
                on_batch={
                    2: lambda: server.notify("turn/completed", {"turnId": "turn-1", "outputText": "late answer"}),
                },
            )
            app_lock = FakeAppLock()

            with patch("runtime.service.time.sleep", side_effect=[None, KeyboardInterrupt()]):
                with (
                    patch("runtime.service.TelegramClient", return_value=telegram),
                    patch("runtime.service.prepare_service_lock", return_value=(app_lock, object())),
                ):
                    with self.assertRaises(KeyboardInterrupt):
                        run_service(paths, start_codex_session_fn=start_fn)

            sessions = SessionStore(paths).list_telegram_sessions(
                AuthState(bot_token="token", telegram_user_id=11, telegram_chat_id=22, paired_at="now")
            )
            self.assertEqual(len(sessions), 2)
            self.assertEqual(sum(1 for session in sessions if session.attached), 1)
            current = next(session for session in sessions if session.attached)
            self.assertEqual(current.status, "ACTIVE")
            self.assertTrue(any(text.startswith("Started new session ") for _, text in telegram.messages))
            self.assertFalse(any(text == "late answer" for _, text in telegram.messages))
            self.assertTrue(any("hidden_session_output_consumed" in message for message in load_recovery_messages(paths)))
            self.assertTrue(app_lock.cleared)

    def test_run_service_normalizes_recovering_turn_on_startup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            save_json(paths.config, Config(state_dir=str(paths.root)).to_dict())
            auth = AuthState(bot_token="token", telegram_user_id=11, telegram_chat_id=22, paired_at="now")
            save_json(paths.auth, auth.to_dict())
            store = SessionStore(paths)
            session = store.get_or_create_telegram_session(auth)
            session.thread_id = "thread-1"
            session.active_turn_id = "turn-1"
            session.status = "RUNNING_TURN"
            store.save_session(session)

            transport = InMemoryJsonRpcTransport()
            server = FakeAppServer(transport)
            server.on("initialize", lambda payload: {"protocolVersion": "1.0", "capabilities": {"threads": True}})
            server.on("getAccount", lambda payload: {"status": "ready", "accountType": "chatgpt"})
            start_fn = make_app_server_start_fn(paths, lambda config, auth: transport)

            telegram = SequentialTelegramClient(
                batches=[
                    [{"update_id": 1, "message": {"chat": {"id": 22}, "from": {"id": 11}, "text": "hello"}}],
                ]
            )
            app_lock = FakeAppLock()

            self._run_service_once(paths, telegram, start_fn, app_lock)

            self.assertEqual(telegram.messages, [])
            current = SessionStore(paths).get_current_telegram_session(auth)
            self.assertIsNotNone(current)
            assert current is not None
            self.assertEqual(current.status, "RUNNING_TURN")
            self.assertEqual(current.active_turn_id, "turn-1")
            self.assertEqual(current.thread_id, "thread-1")
            self.assertTrue(app_lock.cleared)

    def test_run_service_drains_codex_events_even_when_no_telegram_updates_arrive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            auth = AuthState(bot_token="token", telegram_user_id=11, telegram_chat_id=22, paired_at="now")
            save_json(paths.config, Config(state_dir=str(paths.root)).to_dict())
            save_json(paths.auth, auth.to_dict())
            transport = InMemoryJsonRpcTransport()
            server = FakeAppServer(transport)
            server.on("initialize", lambda payload: {"protocolVersion": "1.0", "capabilities": {"threads": True}})
            server.on("getAccount", lambda payload: {"status": "ready", "accountType": "chatgpt"})
            start_fn = make_app_server_start_fn(paths, lambda config, auth: transport)
            telegram = SequentialTelegramClient(batches=[[]])
            app_lock = FakeAppLock()

            drain_calls: list[str] = []

            def record_approvals(*args, **kwargs):
                drain_calls.append("approvals")

            def record_notifications(*args, **kwargs):
                drain_calls.append("notifications")

            with (
                patch("runtime.service.TelegramClient", return_value=telegram),
                patch("runtime.service.prepare_service_lock", return_value=(app_lock, object())),
                patch("runtime.service.time.sleep", side_effect=KeyboardInterrupt()),
                patch("runtime.service.drain_codex_approvals", side_effect=record_approvals),
                patch("runtime.service.drain_codex_notifications", side_effect=record_notifications),
            ):
                with self.assertRaises(KeyboardInterrupt):
                    run_service(paths, start_codex_session_fn=start_fn)

            self.assertIn("approvals", drain_calls)
            self.assertIn("notifications", drain_calls)

    def test_run_service_flushes_idle_partial_output_without_partial_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            auth = AuthState(bot_token="token", telegram_user_id=11, telegram_chat_id=22, paired_at="now")
            save_json(paths.config, Config(state_dir=str(paths.root), partial_flush_idle_seconds=3.0).to_dict())
            save_json(paths.auth, auth.to_dict())
            store = SessionStore(paths)
            session = store.get_or_create_telegram_session(auth)
            session.pending_output_text = "Buffered hello"
            session.pending_output_updated_at = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
            store.save_session(session)

            transport = InMemoryJsonRpcTransport()
            server = FakeAppServer(transport)
            server.on("initialize", lambda payload: {"protocolVersion": "1.0", "capabilities": {"threads": True}})
            server.on("getAccount", lambda payload: {"status": "ready", "accountType": "chatgpt"})
            start_fn = make_app_server_start_fn(paths, lambda config, auth: transport)
            telegram = SequentialTelegramClient(batches=[[]])
            app_lock = FakeAppLock()

            self._run_service_once(paths, telegram, start_fn, app_lock)

            self.assertEqual(telegram.messages, [(22, "Buffered hello")])
            updated = SessionStore(paths).get_or_create_telegram_session(auth)
            self.assertEqual(updated.pending_output_text, "")
            self.assertEqual(updated.last_delivered_output_text, "Buffered hello")

    def test_run_service_sends_typing_indicator_for_active_turn_while_idle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            auth = AuthState(bot_token="token", telegram_user_id=11, telegram_chat_id=22, paired_at="now")
            save_json(
                paths.config,
                Config(
                    state_dir=str(paths.root),
                    typing_indicator_interval_seconds=1.0,
                    poll_interval_seconds=0.1,
                ).to_dict(),
            )
            save_json(paths.auth, auth.to_dict())

            transport = InMemoryJsonRpcTransport()
            server = FakeAppServer(transport)
            server.on("initialize", lambda payload: {"protocolVersion": "1.0", "capabilities": {"threads": True}})
            server.on("getAccount", lambda payload: {"status": "ready", "accountType": "chatgpt"})
            server.on("thread/start", lambda payload: {"threadId": "thread-1"})
            server.on("turn/start", lambda payload: {"turnId": "turn-1"})
            start_fn = make_app_server_start_fn(paths, lambda config, auth: transport)
            telegram = SequentialTelegramClient(
                batches=[
                    [{"update_id": 1, "message": {"chat": {"id": 22}, "from": {"id": 11}, "text": "hello"}}],
                    [],
                ]
            )
            app_lock = FakeAppLock()

            with patch("runtime.service.time.sleep", side_effect=[None, KeyboardInterrupt()]):
                with (
                    patch("runtime.service.TelegramClient", return_value=telegram),
                    patch("runtime.service.prepare_service_lock", return_value=(app_lock, object())),
                ):
                    with self.assertRaises(KeyboardInterrupt):
                        run_service(paths, start_codex_session_fn=start_fn)

            self.assertTrue(telegram.typing_actions)
            self.assertTrue(all(chat_id == 22 for chat_id in telegram.typing_actions))

    def test_run_service_restarts_codex_after_child_exit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            auth = AuthState(bot_token="token", telegram_user_id=11, telegram_chat_id=22, paired_at="now")
            save_json(
                paths.config,
                Config(
                    state_dir=str(paths.root),
                    codex_restart_backoff_seconds=0.0,
                    codex_restart_backoff_max_seconds=0.0,
                ).to_dict(),
            )
            save_json(paths.auth, auth.to_dict())
            telegram = SequentialTelegramClient(batches=[[]])
            app_lock = FakeAppLock()
            start_calls: list[str] = []
            first = RestartingFakeCodex(alive=False)
            second = RestartingFakeCodex(alive=True)

            def start_fn(config, auth_state, runtime, runtime_state, metadata, lock, telegram_client, handle_output):
                start_calls.append("start")
                runtime.set_codex_state("RUNNING")
                if len(start_calls) == 1:
                    return first
                return second

            self._run_service_once(paths, telegram, start_fn, app_lock)

            self.assertEqual(start_calls, ["start", "start"])
            self.assertEqual(telegram.messages, [])
            self.assertTrue(first.stopped)

    def test_maintain_codex_runtime_enters_backoff_when_restart_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            config = Config(
                state_dir=str(paths.root),
                codex_restart_backoff_seconds=2.0,
                codex_restart_backoff_max_seconds=10.0,
            )
            auth = AuthState(bot_token="token", telegram_user_id=11, telegram_chat_id=22, paired_at="now")
            runtime_state = RuntimeState(
                session_id="1",
                service_state="RUNNING",
                codex_state="RUNNING",
                telegram_state="RUNNING",
                recorder_state="RUNNING",
                debug_state="RUNNING",
            )
            runtime = ServiceRuntime(runtime_state)
            telegram = SequentialTelegramClient(batches=[])
            dead = RestartingFakeCodex(alive=False)

            restarted, failures, next_restart_at = maintain_codex_runtime(
                paths=paths,
                config=config,
                auth=auth,
                runtime=runtime,
                runtime_state=runtime_state,
                metadata=object(),
                app_lock=object(),
                telegram=telegram,
                handle_output=lambda source, line: None,
                codex=dead,
                start_codex_session_fn=lambda *args, **kwargs: None,
                restart_failures=0,
                next_restart_at=0.0,
            )

            self.assertIsNone(restarted)
            self.assertEqual(failures, 1)
            self.assertGreater(next_restart_at, 0.0)
            self.assertEqual(runtime_state.codex_state, "BACKOFF")
            self.assertTrue(dead.stopped)

    def test_run_service_marks_pending_approvals_stale_on_boot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            auth = AuthState(bot_token="token", telegram_user_id=11, telegram_chat_id=22, paired_at="now")
            save_json(paths.config, Config(state_dir=str(paths.root)).to_dict())
            save_json(paths.auth, auth.to_dict())
            ApprovalStore(paths).add(ApprovalRecord(17, "approval/request", {"tool": "shell"}))

            transport = InMemoryJsonRpcTransport()
            server = FakeAppServer(transport)
            server.on("initialize", lambda payload: {"protocolVersion": "1.0", "capabilities": {"threads": True}})
            server.on("getAccount", lambda payload: {"status": "ready", "accountType": "chatgpt"})
            start_fn = make_app_server_start_fn(paths, lambda config, auth: transport)
            telegram = SequentialTelegramClient(batches=[[]])
            app_lock = FakeAppLock()

            self._run_service_once(paths, telegram, start_fn, app_lock)

            stale = ApprovalStore(paths).stale()
            self.assertEqual(len(stale), 1)
            self.assertEqual(stale[0].request_id, 17)
            self.assertEqual(telegram.messages, [])

    def test_run_service_enters_telegram_backoff_on_poll_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            auth = AuthState(bot_token="token", telegram_user_id=11, telegram_chat_id=22, paired_at="now")
            save_json(
                paths.config,
                Config(
                    state_dir=str(paths.root),
                    telegram_backoff_seconds=5.0,
                    telegram_backoff_max_seconds=5.0,
                ).to_dict(),
            )
            save_json(paths.auth, auth.to_dict())
            transport = InMemoryJsonRpcTransport()
            server = FakeAppServer(transport)
            server.on("initialize", lambda payload: {"protocolVersion": "1.0", "capabilities": {"threads": True}})
            server.on("getAccount", lambda payload: {"status": "ready", "accountType": "chatgpt"})
            start_fn = make_app_server_start_fn(paths, lambda config, auth: transport)
            telegram = FlakyTelegramClient([TelegramError("network down")])
            app_lock = FakeAppLock()

            self._run_service_once(paths, telegram, start_fn, app_lock)

            runtime = load_runtime_state(paths)
            self.assertIsNotNone(runtime)
            assert runtime is not None
            self.assertEqual(runtime.telegram_state, "BACKOFF")
            self.assertEqual(runtime.codex_state, "RUNNING")
            self.assertNotIn("Tele Cli service connected to Codex App Server.", [text for _, text in telegram.messages])

    def test_run_service_recovers_telegram_polling_after_backoff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            auth = AuthState(bot_token="token", telegram_user_id=11, telegram_chat_id=22, paired_at="now")
            save_json(
                paths.config,
                Config(
                    state_dir=str(paths.root),
                    telegram_backoff_seconds=0.0,
                    telegram_backoff_max_seconds=0.0,
                ).to_dict(),
            )
            save_json(paths.auth, auth.to_dict())
            transport = InMemoryJsonRpcTransport()
            server = FakeAppServer(transport)
            server.on("initialize", lambda payload: {"protocolVersion": "1.0", "capabilities": {"threads": True}})
            server.on("getAccount", lambda payload: {"status": "ready", "accountType": "chatgpt"})
            start_fn = make_app_server_start_fn(paths, lambda config, auth: transport)
            telegram = FlakyTelegramClient(
                [
                    TelegramError("network down"),
                    [],
                ]
            )
            app_lock = FakeAppLock()

            with patch("runtime.service.time.sleep", side_effect=[None, KeyboardInterrupt()]):
                with (
                    patch("runtime.service.TelegramClient", return_value=telegram),
                    patch("runtime.service.prepare_service_lock", return_value=(app_lock, object())),
                ):
                    with self.assertRaises(KeyboardInterrupt):
                        run_service(paths, start_codex_session_fn=start_fn)

            runtime = load_runtime_state(paths)
            self.assertIsNotNone(runtime)
            assert runtime is not None
            self.assertEqual(runtime.telegram_state, "RUNNING")

    def test_run_service_recovers_after_unexpected_telegram_poll_exception(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            auth = AuthState(bot_token="token", telegram_user_id=11, telegram_chat_id=22, paired_at="now")
            save_json(
                paths.config,
                Config(
                    state_dir=str(paths.root),
                    telegram_backoff_seconds=0.0,
                    telegram_backoff_max_seconds=0.0,
                ).to_dict(),
            )
            save_json(paths.auth, auth.to_dict())
            transport = InMemoryJsonRpcTransport()
            server = FakeAppServer(transport)
            server.on("initialize", lambda payload: {"protocolVersion": "1.0", "capabilities": {"threads": True}})
            server.on("getAccount", lambda payload: {"status": "ready", "accountType": "chatgpt"})
            server.on("thread/start", lambda payload: {"threadId": "thread-1"})
            server.on("turn/start", lambda payload: {"turnId": "turn-1"})
            start_fn = make_app_server_start_fn(paths, lambda config, auth: transport)
            telegram = CrashingTelegramClient(
                [
                    ValueError("bad json"),
                    [{"update_id": 1, "message": {"chat": {"id": 22}, "from": {"id": 11}, "text": "hello"}}],
                ]
            )
            app_lock = FakeAppLock()

            with patch("runtime.service.time.sleep", side_effect=[None, KeyboardInterrupt()]):
                with (
                    patch("runtime.service.TelegramClient", return_value=telegram),
                    patch("runtime.service.prepare_service_lock", return_value=(app_lock, object())),
                ):
                    with self.assertRaises(KeyboardInterrupt):
                        run_service(paths, start_codex_session_fn=start_fn)

            runtime = load_runtime_state(paths)
            self.assertIsNotNone(runtime)
            assert runtime is not None
            self.assertEqual(runtime.telegram_state, "RUNNING")
            self.assertTrue(TelegramUpdateStore(paths).has_processed(1))
            event_types = load_event_types(paths)
            self.assertIn("telegram.poll.poll_crash", event_types)
            self.assertIn("telegram.poll.update_enqueued", event_types)

    def test_run_service_does_not_block_startup_on_stale_inflight_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            auth = AuthState(bot_token="token", telegram_user_id=11, telegram_chat_id=22, paired_at="now")
            save_json(paths.config, Config(state_dir=str(paths.root)).to_dict())
            save_json(paths.auth, auth.to_dict())

            store = SessionStore(paths)
            local = store.get_or_create_local_session("smoke-probe")
            local.thread_id = "thread-stale"
            local.active_turn_id = "turn-stale"
            local.status = "RUNNING_TURN"
            local.last_user_message_at = "2026-03-27T00:00:00+00:00"
            store.save_session(local)

            transport = InMemoryJsonRpcTransport()
            server = FakeAppServer(transport)
            server.on("initialize", lambda payload: {"protocolVersion": "1.0", "capabilities": {"threads": True}})
            server.on("getAccount", lambda payload: {"status": "ready", "accountType": "chatgpt"})
            server.on("thread/start", lambda payload: {"threadId": "thread-1"})
            server.on("turn/start", lambda payload: {"turnId": "turn-1"})
            start_fn = make_app_server_start_fn(paths, lambda config, auth: transport)
            telegram = SequentialTelegramClient(
                batches=[
                    [{"update_id": 1, "message": {"chat": {"id": 22}, "from": {"id": 11}, "text": "hello"}}],
                ]
            )
            app_lock = FakeAppLock()

            self._run_service_once(paths, telegram, start_fn, app_lock)

            runtime = load_runtime_state(paths)
            self.assertIsNotNone(runtime)
            assert runtime is not None
            self.assertEqual(runtime.service_state, "RUNNING")
            self.assertTrue(TelegramUpdateStore(paths).has_processed(1))
            session = SessionStore(paths).get_current_telegram_session(auth)
            self.assertIsNotNone(session)
            assert session is not None
            self.assertEqual(session.status, "RUNNING_TURN")
            self.assertEqual(session.thread_id, "thread-1")

    def test_run_service_writes_runtime_before_startup_sleep(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            auth = AuthState(bot_token="token", telegram_user_id=11, telegram_chat_id=22, paired_at="now")
            save_json(paths.config, Config(state_dir=str(paths.root), sleep_hour_local=2).to_dict())
            save_json(paths.auth, auth.to_dict())
            memory_dir = paths.root / "memory" / "sessions"
            memory_dir.mkdir(parents=True, exist_ok=True)
            memory_dir.joinpath("session-1.short_memory.md").write_text("- pending\n", encoding="utf-8")

            transport = InMemoryJsonRpcTransport()
            server = FakeAppServer(transport)
            server.on("initialize", lambda payload: {"protocolVersion": "1.0", "capabilities": {"threads": True}})
            server.on("getAccount", lambda payload: {"status": "ready", "accountType": "chatgpt"})
            start_fn = make_app_server_start_fn(paths, lambda config, auth: transport)
            telegram = SequentialTelegramClient(batches=[[]])
            app_lock = FakeAppLock()

            def crash_sleep(*args, **kwargs):
                runtime = load_runtime_state(paths)
                self.assertIsNotNone(runtime)
                assert runtime is not None
                self.assertEqual(runtime.service_state, "RUNNING")
                raise KeyboardInterrupt()

            with (
                patch("runtime.service.TelegramClient", return_value=telegram),
                patch("runtime.service.prepare_service_lock", return_value=(app_lock, object())),
                patch("runtime.service.run_sleep", side_effect=crash_sleep),
            ):
                with self.assertRaises(KeyboardInterrupt):
                    run_service(paths, start_codex_session_fn=start_fn)


if __name__ == "__main__":
    unittest.main()
