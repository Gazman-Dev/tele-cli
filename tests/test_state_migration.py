from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.models import CodexServerState
from core.paths import build_paths
from core.state_versions import STATE_SCHEMA_VERSION, StateMigrationError, load_versioned_state
from runtime.approval_store import ApprovalRecord, ApprovalStore
from runtime.session_store import SessionStore


class StateMigrationTests(unittest.TestCase):
    def test_session_store_migrates_legacy_unversioned_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            paths.sessions.write_text(
                json.dumps(
                    {
                        "sessions": [
                            {
                                "session_id": "s1",
                                "transport": "telegram",
                                "transport_user_id": 11,
                                "transport_chat_id": 22,
                                "attached": True,
                                "status": "ACTIVE",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            sessions = SessionStore(paths).load().sessions
            payload = json.loads(paths.sessions.read_text(encoding="utf-8"))

        self.assertEqual(len(sessions), 1)
        self.assertEqual(payload["version"], STATE_SCHEMA_VERSION)
        self.assertIn("payload", payload)

    def test_approval_store_migrates_legacy_unversioned_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            paths.approvals.write_text(
                json.dumps(
                    {
                        "approvals": [
                            {
                                "request_id": 17,
                                "method": "approval/request",
                                "params": {"tool": "shell"},
                                "status": "pending",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            approvals = ApprovalStore(paths).pending()
            payload = json.loads(paths.approvals.read_text(encoding="utf-8"))

        self.assertEqual(len(approvals), 1)
        self.assertEqual(approvals[0].request_id, 17)
        self.assertEqual(payload["version"], STATE_SCHEMA_VERSION)

    def test_codex_server_state_rejects_future_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            paths.codex_server.write_text(
                json.dumps(
                    {
                        "version": STATE_SCHEMA_VERSION + 1,
                        "payload": {
                            "transport": "stdio://",
                            "initialized": True,
                        },
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaises(StateMigrationError):
                load_versioned_state(paths.codex_server, CodexServerState.from_dict)

    def test_saving_approval_store_uses_versioned_envelope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            store = ApprovalStore(paths)
            store.add(ApprovalRecord(17, "approval/request", {"tool": "shell"}))

            payload = json.loads(paths.approvals.read_text(encoding="utf-8"))

        self.assertEqual(payload["version"], STATE_SCHEMA_VERSION)
        self.assertIn("payload", payload)


if __name__ == "__main__":
    unittest.main()
