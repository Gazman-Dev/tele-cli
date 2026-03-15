from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.json_store import save_json
from core.models import AuthState, Config
from core.paths import build_paths
from runtime.app_server_runtime import make_app_server_start_fn
from runtime.service import run_service
from runtime.session_store import SessionStore
from tests.fakes.fake_app_server import FakeAppServer, InMemoryJsonRpcTransport


class FakeAppLock:
    def __init__(self) -> None:
        self.cleared = False

    def clear(self) -> None:
        self.cleared = True


class SequentialTelegramClient:
    def __init__(self, batches: list[list[dict]], on_batch: dict[int, callable] | None = None) -> None:
        self._batches = list(batches)
        self._calls = 0
        self._on_batch = on_batch or {}
        self.messages: list[tuple[int, str]] = []

    def get_updates(self, offset=None, timeout: int = 20) -> list[dict]:
        self._calls += 1
        callback = self._on_batch.get(self._calls)
        if callback is not None:
            callback()
        if self._batches:
            return self._batches.pop(0)
        return []

    def send_message(self, chat_id: int, text: str) -> None:
        self.messages.append((chat_id, text))


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
            self.assertEqual(len(sessions), 1)
            self.assertTrue(sessions[0].attached)
            self.assertEqual(sessions[0].status, "ACTIVE")
            self.assertEqual(
                telegram.messages[0],
                (22, "Tele Cli service connected to Codex App Server."),
            )
            self.assertTrue(any(text.startswith("Started new session ") for _, text in telegram.messages))
            self.assertFalse(any(text == "late answer" for _, text in telegram.messages))
            recovery_log = paths.recovery_log.read_text(encoding="utf-8")
            self.assertIn("hidden_session_output_consumed", recovery_log)
            self.assertIn("detached_sessions_pruned count=1", recovery_log)
            self.assertTrue(app_lock.cleared)

    def test_run_service_reports_and_blocks_unresolved_recovering_turn(self) -> None:
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
            server.on("thread/resume", lambda payload: (_ for _ in ()).throw(RuntimeError("resume failed")))
            start_fn = make_app_server_start_fn(paths, lambda config, auth: transport)

            telegram = SequentialTelegramClient(
                batches=[
                    [{"update_id": 1, "message": {"chat": {"id": 22}, "from": {"id": 11}, "text": "hello"}}],
                ]
            )
            app_lock = FakeAppLock()

            self._run_service_once(paths, telegram, start_fn, app_lock)

            self.assertEqual(
                telegram.messages[:3],
                [
                    (22, "Tele Cli service connected to Codex App Server."),
                    (
                        22,
                        "A previous turn is still recovering after restart. This chat stays blocked until recovery finishes, /stop is used, or /new starts fresh.",
                    ),
                    (
                        22,
                        "Current session is recovering an in-flight turn. Wait for recovery, use /stop, or start fresh with /new.",
                    ),
                ],
            )
            current = SessionStore(paths).get_current_telegram_session(auth)
            self.assertIsNotNone(current)
            assert current is not None
            self.assertEqual(current.status, "RECOVERING_TURN")
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


if __name__ == "__main__":
    unittest.main()
