from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from minic.json_store import save_json
from minic.models import AuthState, RuntimeState, SetupState
from minic.paths import build_paths
from minic.runtime import ServiceRuntime
from minic.service import reset_auth
from minic.setup_flow import _handle_existing_setup
from minic.telegram import confirm_pairing_code, register_pairing_request


class RuntimeTests(unittest.TestCase):
    def test_runtime_rejects_duplicate_start(self) -> None:
        runtime = ServiceRuntime(
            RuntimeState(
                session_id="1",
                service_state="RUNNING",
                codex_state="STOPPED",
                telegram_state="STOPPED",
                recorder_state="STOPPED",
                debug_state="STOPPED",
            )
        )
        runtime.start_codex()
        with self.assertRaises(RuntimeError):
            runtime.start_codex()

    def test_reset_auth_clears_pairing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            save_json(
                paths.auth,
                AuthState(
                    bot_token="token",
                    telegram_user_id=1,
                    telegram_chat_id=2,
                    pairing_code="12345",
                    paired_at="now",
                ).to_dict(),
            )
            reset_auth(paths)
            data = paths.auth.read_text(encoding="utf-8")
            self.assertIn('"telegram_user_id": null', data)
            self.assertIn('"telegram_chat_id": null', data)

    def test_pairing_requires_terminal_confirmation(self) -> None:
        auth = AuthState(bot_token="token")
        update = {"message": {"chat": {"id": 22}, "from": {"id": 11}, "text": "hello"}}
        ok, status = register_pairing_request(auth, update)
        self.assertFalse(ok)
        self.assertEqual(status, "code-issued")
        self.assertEqual(auth.pending_chat_id, 22)
        self.assertEqual(auth.pending_user_id, 11)
        self.assertIsNotNone(auth.pairing_code)

        self.assertFalse(confirm_pairing_code(auth, "wrong"))
        self.assertTrue(confirm_pairing_code(auth, auth.pairing_code))
        self.assertEqual(auth.telegram_chat_id, 22)
        self.assertEqual(auth.telegram_user_id, 11)

    def test_completed_setup_does_not_trigger_recovery_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            save_json(
                paths.setup_lock,
                SetupState(status="completed", pid=123, timestamp="now").to_dict(),
            )
            state = _handle_existing_setup(paths)
            self.assertEqual(state.status, "started")
            self.assertEqual(state.pid, 0)


if __name__ == "__main__":
    unittest.main()
