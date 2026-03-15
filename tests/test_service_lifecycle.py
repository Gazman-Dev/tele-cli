from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from core.locks import LockInspection
from core.models import AuthState, LockMetadata, RuntimeState
from core.paths import build_paths
from runtime.approval_store import ApprovalRecord, ApprovalStore
from runtime.control import classify_service_conflict, handle_service_conflict
from runtime.service import build_status_message, handle_authorized_message


class FakeLockFile:
    def __init__(self, inspection: LockInspection):
        self._inspection = inspection
        self.cleared = False

    def inspect(self) -> LockInspection:
        return self._inspection

    def clear(self) -> None:
        self.cleared = True


class FakeTelegram:
    def __init__(self) -> None:
        self.messages: list[tuple[int, str]] = []

    def send_message(self, chat_id: int, text: str) -> None:
        self.messages.append((chat_id, text))


class FakeRecorder:
    def __init__(self) -> None:
        self.records: list[tuple[str, str]] = []

    def record(self, source: str, line: str) -> None:
        self.records.append((source, line))


class FakeCodex:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def send(self, text: str) -> None:
        self.sent.append(text)


class ServiceLifecycleTests(unittest.TestCase):
    def test_classify_service_conflict_recognizes_live_unknown(self) -> None:
        inspection = LockInspection(
            exists=True,
            live=True,
            same_app=False,
            metadata=LockMetadata(
                pid=100,
                hostname="host",
                username="user",
                started_at="now",
                mode="service",
                timestamp="now",
                app_version="1",
            ),
        )
        self.assertEqual(classify_service_conflict(inspection), "live_unknown")

    def test_background_service_exits_on_live_owner_conflict(self) -> None:
        paths = build_paths(Path.cwd() / ".test_state" / "service_lifecycle_live")
        lock = FakeLockFile(
            LockInspection(
                exists=True,
                live=True,
                same_app=True,
                metadata=LockMetadata(
                    pid=123,
                    hostname="host",
                    username="user",
                    started_at="now",
                    mode="service",
                    timestamp="now",
                    app_version="1",
                ),
            )
        )
        with (
            patch("runtime.control.isatty", return_value=False),
            patch("runtime.control.append_recovery_log"),
        ):
            with self.assertRaises(SystemExit):
                handle_service_conflict(paths, lock)
        self.assertFalse(lock.cleared)

    def test_background_service_auto_heals_stale_lock(self) -> None:
        paths = build_paths(Path.cwd() / ".test_state" / "service_lifecycle_stale")
        lock = FakeLockFile(
            LockInspection(
                exists=True,
                live=False,
                same_app=False,
                metadata=LockMetadata(
                    pid=123,
                    hostname="host",
                    username="user",
                    started_at="now",
                    mode="service",
                    timestamp="now",
                    app_version="1",
                ),
            )
        )
        with (
            patch("runtime.control.isatty", return_value=False),
            patch("runtime.control.append_recovery_log"),
        ):
            handle_service_conflict(paths, lock)
        self.assertTrue(lock.cleared)

    def test_status_message_reports_degraded_codex_readiness(self) -> None:
        auth = AuthState(
            bot_token="token",
            telegram_user_id=11,
            telegram_chat_id=22,
            paired_at="now",
        )
        paths = build_paths(Path.cwd() / ".test_state" / "service_lifecycle_status")
        ApprovalStore(paths).add(ApprovalRecord(17, "approval/request", {"tool": "shell"}, status="stale"))
        runtime_state = RuntimeState(
            session_id="1",
            service_state="RUNNING",
            codex_state="AUTH_REQUIRED",
            telegram_state="RUNNING",
            recorder_state="RUNNING",
            debug_state="RUNNING",
        )
        from runtime.session_store import SessionStore

        status = build_status_message(auth, runtime_state, SessionStore(paths))
        self.assertIn("service=RUNNING", status)
        self.assertIn("telegram=RUNNING", status)
        self.assertIn("codex=AUTH_REQUIRED", status)
        self.assertIn("pairing=paired chat=22 user=11", status)
        self.assertIn("stale_approvals=1", status)

    def test_status_command_works_without_codex(self) -> None:
        auth = AuthState(
            bot_token="token",
            telegram_user_id=11,
            telegram_chat_id=22,
            paired_at="now",
        )
        runtime_state = RuntimeState(
            session_id="1",
            service_state="RUNNING",
            codex_state="DEGRADED",
            telegram_state="RUNNING",
            recorder_state="RUNNING",
            debug_state="RUNNING",
        )
        telegram = FakeTelegram()
        recorder = FakeRecorder()

        handle_authorized_message("/status", auth, runtime_state, None, telegram, recorder)

        self.assertEqual(len(telegram.messages), 1)
        self.assertEqual(telegram.messages[0][0], 22)
        self.assertIn("codex=DEGRADED", telegram.messages[0][1])
        self.assertEqual(recorder.records, [])

    def test_non_status_message_reports_codex_not_ready(self) -> None:
        auth = AuthState(
            bot_token="token",
            telegram_user_id=11,
            telegram_chat_id=22,
            paired_at="now",
        )
        runtime_state = RuntimeState(
            session_id="1",
            service_state="RUNNING",
            codex_state="STOPPED",
            telegram_state="RUNNING",
            recorder_state="RUNNING",
            debug_state="RUNNING",
        )
        telegram = FakeTelegram()
        recorder = FakeRecorder()

        handle_authorized_message("hello", auth, runtime_state, None, telegram, recorder)

        self.assertEqual(telegram.messages, [(22, "Codex is not ready yet.")])
        self.assertEqual(recorder.records, [])

    def test_regular_message_is_forwarded_to_codex(self) -> None:
        auth = AuthState(
            bot_token="token",
            telegram_user_id=11,
            telegram_chat_id=22,
            paired_at="now",
        )
        runtime_state = RuntimeState(
            session_id="1",
            service_state="RUNNING",
            codex_state="RUNNING",
            telegram_state="RUNNING",
            recorder_state="RUNNING",
            debug_state="RUNNING",
        )
        telegram = FakeTelegram()
        recorder = FakeRecorder()
        codex = FakeCodex()

        handle_authorized_message("hello", auth, runtime_state, codex, telegram, recorder)

        self.assertEqual(codex.sent, ["hello"])
        self.assertEqual(recorder.records, [("telegram", "hello")])
        self.assertEqual(telegram.messages, [])


if __name__ == "__main__":
    unittest.main()
