from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.json_store import save_json
from core.models import CodexServerState, RuntimeState
from core.paths import build_paths
from runtime.approval_store import ApprovalRecord, ApprovalStore
from runtime.session_store import SessionStore
from runtime.telegram_update_store import TelegramUpdateStore
from storage.db import StorageManager
from storage.operations import ServiceRunStore, TraceStore
from storage.runtime_state_store import (
    load_codex_server_state,
    load_runtime_state,
    save_codex_server_state,
    save_runtime_state,
)


class SqliteMigrationTests(unittest.TestCase):
    def test_storage_initialization_creates_database_and_schema_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            StorageManager(paths)

            self.assertTrue(paths.database.exists())
            with sqlite3.connect(paths.database) as connection:
                row = connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()

            self.assertIsNotNone(row)
            self.assertGreater(row[0], 0)

    def test_session_store_persists_into_sqlite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            store = SessionStore(paths)

            session = store.get_or_create_local_session("main")
            session.thread_id = "thread-1"
            store.save_session(session)

            with sqlite3.connect(paths.database) as connection:
                row = connection.execute(
                    "SELECT transport, transport_channel, thread_id FROM sessions WHERE session_id = ?",
                    (session.session_id,),
                ).fetchone()

            self.assertEqual(row, ("local", "main", "thread-1"))

    def test_approval_store_persists_into_sqlite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            store = ApprovalStore(paths)
            store.add(ApprovalRecord(17, "approval/request", {"tool": "shell"}))

            with sqlite3.connect(paths.database) as connection:
                row = connection.execute("SELECT request_id, status FROM approvals WHERE request_id = 17").fetchone()

            self.assertEqual(row, (17, "pending"))

    def test_runtime_state_round_trips_through_sqlite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            runtime_state = RuntimeState(
                session_id="run-1",
                service_state="RUNNING",
                codex_state="STOPPED",
                telegram_state="RUNNING",
                recorder_state="STOPPED",
                debug_state="STOPPED",
            )
            codex_server_state = CodexServerState(transport="stdio://", initialized=True, protocol_version="1")

            save_runtime_state(paths, runtime_state)
            save_codex_server_state(paths, codex_server_state)

            loaded_runtime = load_runtime_state(paths)
            loaded_codex = load_codex_server_state(paths)

            self.assertIsNotNone(loaded_runtime)
            self.assertIsNotNone(loaded_codex)
            assert loaded_runtime is not None
            assert loaded_codex is not None
            self.assertEqual(loaded_runtime.session_id, "run-1")
            self.assertEqual(loaded_codex.protocol_version, "1")

    def test_storage_bootstraps_legacy_json_state_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            large_approval_params = {"tool": "shell", "output": "x" * 9000}
            save_json(
                paths.root / "sessions.json",
                {
                    "sessions": [
                        {
                            "session_id": "legacy-session",
                            "transport": "local",
                            "transport_user_id": None,
                            "transport_chat_id": None,
                            "transport_channel": "main",
                            "attached": True,
                            "status": "ACTIVE",
                            "instructions_dirty": True,
                            "last_seen_generation": 0,
                            "created_at": "2026-03-01T00:00:00+00:00",
                        }
                    ]
                },
            )
            save_json(
                paths.root / "approvals.json",
                {"approvals": [{"request_id": 9, "method": "approval/request", "params": large_approval_params}]},
            )
            save_json(paths.root / "telegram_updates.json", {"processed_update_ids": [77]})
            save_json(
                paths.root / "runtime.json",
                RuntimeState(
                    session_id="legacy-run",
                    service_state="RUNNING",
                    codex_state="RUNNING",
                    telegram_state="RUNNING",
                    recorder_state="RUNNING",
                    debug_state="RUNNING",
                ).to_dict(),
            )
            save_json(paths.root / "codex_server.json", CodexServerState(transport="stdio://", initialized=True).to_dict())

            StorageManager(paths)

            loaded_runtime = load_runtime_state(paths)
            loaded_codex = load_codex_server_state(paths)
            self.assertIsNotNone(loaded_runtime)
            self.assertIsNotNone(loaded_codex)
            assert loaded_runtime is not None
            self.assertEqual(loaded_runtime.session_id, "legacy-run")
            self.assertIsNotNone(SessionStore(paths).get_current_local_session("main"))
            self.assertTrue(TelegramUpdateStore(paths).has_processed(77))
            pending = ApprovalStore(paths).get_pending(9)
            self.assertIsNotNone(pending)
            assert pending is not None
            self.assertEqual(pending.params, large_approval_params)

            with sqlite3.connect(paths.database) as connection:
                row = connection.execute(
                    "SELECT value_json FROM app_state WHERE state_key = 'sqlite_bootstrap'"
                ).fetchone()
                approval_row = connection.execute("SELECT params_json FROM approvals WHERE request_id = 9").fetchone()
                events = connection.execute(
                    "SELECT event_type FROM events WHERE source = 'storage' ORDER BY event_id"
                ).fetchall()

            self.assertIsNotNone(row)
            self.assertIn("sessions.json", row[0])
            self.assertIn('"storage":"artifact"', approval_row[0])
            self.assertIn(("storage.bootstrap.legacy_import",), events)
            self.assertIn(("storage.bootstrap.completed",), events)

    def test_trace_store_spills_large_payloads_to_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            trace_store = TraceStore(paths)

            trace_store.log_event(
                source="service",
                event_type="service.large_payload",
                payload={"text": "x" * 9000},
            )

            with sqlite3.connect(paths.database) as connection:
                row = connection.execute(
                    "SELECT payload_json, payload_preview, artifact_id FROM events ORDER BY event_id DESC LIMIT 1"
                ).fetchone()

            self.assertEqual(row[0], None)
            self.assertTrue(row[1])
            self.assertTrue(row[2])

    def test_service_run_start_only_requeues_claims_from_other_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            storage = StorageManager(paths)
            runs = ServiceRunStore(paths)
            with storage.transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO service_runs(run_id, started_at, version, pid, hostname, state_dir, exit_reason, stopped_at)
                    VALUES ('old-run', '2026-03-01T00:00:00+00:00', 'test', NULL, 'host', ?, 'crashed', NULL)
                    """,
                    (str(paths.root),),
                )
            runs.start(run_id="new-run")
            with storage.transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO telegram_outbound_queue(
                        queue_id, created_at, available_at, status, op_type, chat_id, topic_id, session_id, trace_id,
                        message_group_id, telegram_message_id, dedupe_key, priority, disable_notification, payload_json,
                        attempt_count, last_error, claimed_by_run_id, claimed_at, completed_at
                    ) VALUES (?, ?, ?, 'claimed', 'send_message', 22, NULL, NULL, NULL, NULL, NULL, NULL, 100, 0, ?, 0, NULL, ?, ?, NULL)
                    """,
                    ("claimed-old", "2026-03-01T00:00:00+00:00", "2026-03-01T00:00:00+00:00", '{"text":"hi"}', "old-run", "2026-03-01T00:00:01+00:00"),
                )
                connection.execute(
                    """
                    INSERT INTO telegram_outbound_queue(
                        queue_id, created_at, available_at, status, op_type, chat_id, topic_id, session_id, trace_id,
                        message_group_id, telegram_message_id, dedupe_key, priority, disable_notification, payload_json,
                        attempt_count, last_error, claimed_by_run_id, claimed_at, completed_at
                    ) VALUES (?, ?, ?, 'claimed', 'send_message', 22, NULL, NULL, NULL, NULL, NULL, NULL, 100, 0, ?, 0, NULL, ?, ?, NULL)
                    """,
                    ("claimed-new", "2026-03-01T00:00:00+00:00", "2026-03-01T00:00:00+00:00", '{"text":"hi"}', "new-run", "2026-03-01T00:00:01+00:00"),
                )

            runs.start(run_id="new-run")

            with sqlite3.connect(paths.database) as connection:
                rows = connection.execute(
                    "SELECT queue_id, status, claimed_by_run_id FROM telegram_outbound_queue ORDER BY queue_id"
                ).fetchall()

            self.assertEqual(rows, [("claimed-new", "claimed", "new-run"), ("claimed-old", "queued", None)])

    def test_service_run_start_preserves_claims_from_live_other_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            storage = StorageManager(paths)
            runs = ServiceRunStore(paths)
            with storage.transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO service_runs(run_id, started_at, version, pid, hostname, state_dir, exit_reason, stopped_at)
                    VALUES ('live-run', '2026-03-01T00:00:00+00:00', 'test', 99999, 'host', ?, NULL, NULL)
                    """,
                    (str(paths.root),),
                )
                connection.execute(
                    """
                    INSERT INTO telegram_outbound_queue(
                        queue_id, created_at, available_at, status, op_type, chat_id, topic_id, session_id, trace_id,
                        message_group_id, telegram_message_id, dedupe_key, priority, disable_notification, payload_json,
                        attempt_count, last_error, claimed_by_run_id, claimed_at, completed_at
                    ) VALUES (?, ?, ?, 'claimed', 'send_message', 22, NULL, NULL, NULL, NULL, NULL, NULL, 100, 0, ?, 0, NULL, ?, ?, NULL)
                    """,
                    ("claimed-live", "2026-03-01T00:00:00+00:00", "2026-03-01T00:00:00+00:00", '{"text":"hi"}', "live-run", "2026-03-01T00:00:01+00:00"),
                )

            with patch("storage.operations.process_exists", return_value=True):
                runs.start(run_id="new-run")

            with sqlite3.connect(paths.database) as connection:
                row = connection.execute(
                    "SELECT queue_id, status, claimed_by_run_id FROM telegram_outbound_queue WHERE queue_id = 'claimed-live'"
                ).fetchone()

            self.assertEqual(row, ("claimed-live", "claimed", "live-run"))


if __name__ == "__main__":
    unittest.main()
