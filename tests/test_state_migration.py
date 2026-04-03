from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.json_store import save_json
from core.models import AuthState, CodexServerState, RuntimeState
from core.paths import build_paths
from runtime.performance import PerformanceTracker
from runtime.recorder import Recorder
from runtime.approval_store import ApprovalRecord, ApprovalStore
from runtime.session_store import SessionStore
from runtime.telegram_update_store import TelegramUpdateStore
from storage.db import StorageManager
from storage.logging_health import load_logging_health
from storage.log_maintenance import LogRetentionPolicy, prune_logs
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
            with closing(sqlite3.connect(paths.database)) as connection:
                row = connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()

            self.assertIsNotNone(row)
            self.assertGreater(row[0], 0)

    def test_storage_initialization_creates_workspace_schema(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            StorageManager(paths)

            with closing(sqlite3.connect(paths.database)) as connection:
                workspace_table = connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'workspaces'"
                ).fetchone()
                session_columns = {
                    row[1]
                    for row in connection.execute("PRAGMA table_info(sessions)").fetchall()
                }

            self.assertEqual(workspace_table, ("workspaces",))
            self.assertIn("workspace_relpath", session_columns)
            self.assertIn("agents_md_relpath", session_columns)

    def test_storage_repairs_partial_schema_when_migration_marker_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            paths.root.mkdir(parents=True, exist_ok=True)
            migration_path = Path(__file__).resolve().parents[1] / "src" / "storage" / "migrations" / "0001_initial.sql"
            checksum = hashlib.sha256(migration_path.read_bytes()).hexdigest()
            with closing(sqlite3.connect(paths.database)) as connection:
                connection.execute(
                    """
                    CREATE TABLE schema_migrations (
                        id INTEGER PRIMARY KEY,
                        version INTEGER NOT NULL UNIQUE,
                        name TEXT NOT NULL UNIQUE,
                        checksum TEXT NOT NULL,
                        applied_at TEXT NOT NULL
                    )
                    """
                )
                connection.execute(
                    """
                    INSERT INTO schema_migrations(version, name, checksum, applied_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (1, "0001_initial.sql", checksum, "2026-03-30T00:00:00+00:00"),
                )
                connection.commit()

            StorageManager(paths)

            with closing(sqlite3.connect(paths.database)) as connection:
                app_state_row = connection.execute("SELECT COUNT(*) FROM app_state").fetchone()
                service_runs_row = connection.execute("SELECT COUNT(*) FROM service_runs").fetchone()
                migration_row = connection.execute("SELECT COUNT(*) FROM schema_migrations WHERE version = 1").fetchone()

            self.assertEqual(migration_row[0], 1)
            self.assertEqual(app_state_row[0], 1)
            self.assertEqual(service_runs_row[0], 0)

    def test_session_store_persists_into_sqlite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            store = SessionStore(paths)

            session = store.get_or_create_local_session("main")
            session.thread_id = "thread-1"
            store.save_session(session)

            with closing(sqlite3.connect(paths.database)) as connection:
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

            with closing(sqlite3.connect(paths.database)) as connection:
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
            save_json(
                paths.root / "codex_server.json",
                {
                    "version": 1,
                    "payload": CodexServerState(transport="stdio://", initialized=True).to_dict(),
                },
            )

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

            with closing(sqlite3.connect(paths.database)) as connection:
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

            with closing(sqlite3.connect(paths.database)) as connection:
                row = connection.execute(
                    "SELECT payload_json, payload_preview, artifact_id FROM events ORDER BY event_id DESC LIMIT 1"
                ).fetchone()

            self.assertEqual(row[0], None)
            self.assertTrue(row[1])
            self.assertTrue(row[2])

    def test_recorder_mirrors_terminal_lines_into_sqlite_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            recorder = Recorder(paths.terminal_log, trace_store=TraceStore(paths))

            recorder.start()
            recorder.record("assistant", "hello from terminal mirror")
            recorder.stop()

            with closing(sqlite3.connect(paths.database)) as connection:
                row = connection.execute(
                    "SELECT source, event_type, payload_json FROM events ORDER BY event_id DESC LIMIT 1"
                ).fetchone()

            self.assertEqual(row[0], "terminal")
            self.assertEqual(row[1], "terminal.assistant")
            self.assertIn("hello from terminal mirror", row[2])
            self.assertIn("hello from terminal mirror", paths.terminal_log.read_text(encoding="utf-8"))

    def test_recorder_continues_when_terminal_mirror_write_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            paths.terminal_log.parent.mkdir(parents=True, exist_ok=True)
            paths.terminal_log.mkdir()
            recorder = Recorder(paths.terminal_log, trace_store=TraceStore(paths))

            recorder.start()
            recorder.record("assistant", "still captured")
            recorder.stop()

            with closing(sqlite3.connect(paths.database)) as connection:
                rows = connection.execute(
                    "SELECT source, event_type, payload_json FROM events WHERE event_type IN ('terminal.assistant', 'logging.mirror_write_failed') ORDER BY event_id ASC"
                ).fetchall()

            self.assertEqual(rows[0][0], "terminal")
            self.assertEqual(rows[0][1], "terminal.assistant")
            self.assertEqual(rows[1][0], "storage")
            self.assertEqual(rows[1][1], "logging.mirror_write_failed")

    def test_performance_tracker_mirrors_records_into_sqlite_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            performance = PerformanceTracker(paths.performance_log, trace_store=TraceStore(paths))

            performance.log("telegram_send_completed", session_id="session-1", duration_ms=12.5)

            with closing(sqlite3.connect(paths.database)) as connection:
                row = connection.execute(
                    "SELECT source, event_type, payload_json FROM events ORDER BY event_id DESC LIMIT 1"
                ).fetchone()

            self.assertEqual(row[0], "performance")
            self.assertEqual(row[1], "telegram_send_completed")
            self.assertIn('"duration_ms":12.5', row[2])
            self.assertIn("telegram_send_completed", paths.performance_log.read_text(encoding="utf-8"))

    def test_performance_tracker_continues_when_mirror_write_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            paths.performance_log.parent.mkdir(parents=True, exist_ok=True)
            paths.performance_log.mkdir()
            performance = PerformanceTracker(paths.performance_log, trace_store=TraceStore(paths))

            performance.log("telegram_send_completed", session_id="session-1", duration_ms=12.5)

            with closing(sqlite3.connect(paths.database)) as connection:
                rows = connection.execute(
                    "SELECT source, event_type FROM events WHERE event_type IN ('telegram_send_completed', 'logging.mirror_write_failed') ORDER BY event_id ASC"
                ).fetchall()

            self.assertEqual(rows[0], ("performance", "telegram_send_completed"))
            self.assertEqual(rows[1], ("storage", "logging.mirror_write_failed"))

    def test_session_store_logs_session_lifecycle_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            store = SessionStore(paths)
            auth = AuthState(bot_token="token", telegram_user_id=11, telegram_chat_id=22, paired_at="now")

            first = store.get_or_create_telegram_session(auth, 7)
            second = store.get_or_create_telegram_session(auth, 7)
            replacement = store.create_new_telegram_session(auth, 7)

            self.assertEqual(first.session_id, second.session_id)
            self.assertNotEqual(first.session_id, replacement.session_id)

            with closing(sqlite3.connect(paths.database)) as connection:
                rows = connection.execute(
                    """
                    SELECT source, event_type, session_id, payload_json
                    FROM events
                    WHERE source = 'session'
                    ORDER BY event_id ASC
                    """
                ).fetchall()

            event_types = [str(row[1]) for row in rows]
            self.assertIn("session.created", event_types)
            self.assertIn("session.reused", event_types)
            self.assertIn("session.detached", event_types)
            self.assertIn("session.replaced", event_types)

    def test_trace_store_marks_logging_degraded_when_sqlite_unavailable_and_recovers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            trace_store = TraceStore(paths, run_id="run-1")
            paths.database.unlink(missing_ok=True)
            paths.database.mkdir(parents=True, exist_ok=True)
            trace_store.log_event(source="service", event_type="service.started", payload={"x": 1})

            degraded = load_logging_health(paths)
            self.assertEqual(degraded["state"], "degraded")
            self.assertEqual(degraded["event_type"], "service.started")
            self.assertTrue(paths.logging_emergency_log.exists())

            paths.database.rmdir()
            ServiceRunStore(paths).start(run_id="run-1", pid=123)
            trace_store.log_event(source="service", event_type="service.started", payload={"x": 2})

            recovered = load_logging_health(paths)
            self.assertEqual(recovered["state"], "healthy")

            with closing(sqlite3.connect(paths.database)) as connection:
                rows = connection.execute(
                    """
                    SELECT event_type, payload_json
                    FROM events
                    WHERE event_type IN ('service.started', 'service.degraded', 'service.recovered')
                    ORDER BY event_id ASC
                    """
                ).fetchall()

            event_types = [str(row[0]) for row in rows]
            self.assertIn("service.started", event_types)
            self.assertIn("service.degraded", event_types)
            self.assertIn("service.recovered", event_types)

    def test_prune_logs_deletes_old_completed_event_data_and_rotates_mirrors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            storage = StorageManager(paths)
            trace_store = TraceStore(paths)
            trace_id = trace_store.start_trace(
                session_id=None,
                chat_id=22,
                topic_id=None,
                user_text="old request",
            )
            trace_store.complete_trace(trace_id, outcome="completed")
            with storage.transaction() as connection:
                connection.execute(
                    "UPDATE traces SET started_at = ?, completed_at = ? WHERE trace_id = ?",
                    ("2025-01-01T00:00:00+00:00", "2025-01-01T00:05:00+00:00", trace_id),
                )
                connection.execute(
                    "UPDATE events SET received_at = ?, handled_at = ? WHERE trace_id = ?",
                    ("2025-01-01T00:01:00+00:00", "2025-01-01T00:01:01+00:00", trace_id),
                )
                connection.execute(
                    """
                    INSERT INTO service_runs(run_id, started_at, version, pid, hostname, state_dir, exit_reason, stopped_at)
                    VALUES ('old-run', '2025-01-01T00:00:00+00:00', 'test', NULL, 'host', ?, 'stopped', '2025-01-01T00:10:00+00:00')
                    """,
                    (str(paths.root),),
                )
                connection.execute(
                    """
                    INSERT INTO telegram_outbound_queue(
                        queue_id, created_at, available_at, status, op_type, chat_id, topic_id, session_id, trace_id,
                        message_group_id, telegram_message_id, dedupe_key, priority, disable_notification, payload_json,
                        attempt_count, last_error, claimed_by_run_id, claimed_at, completed_at
                    ) VALUES (?, ?, ?, 'completed', 'send_message', 22, NULL, NULL, NULL, NULL, NULL, NULL, 100, 0, ?, 0, NULL, NULL, NULL, ?)
                    """,
                    ("queue-old", "2025-01-01T00:00:00+00:00", "2025-01-01T00:00:00+00:00", '{"text":"hi"}', "2025-01-01T00:10:00+00:00"),
                )
            paths.terminal_log.parent.mkdir(parents=True, exist_ok=True)
            paths.terminal_log.write_text("x" * 64, encoding="utf-8")
            paths.performance_log.write_text("y" * 64, encoding="utf-8")

            summary = prune_logs(
                paths,
                policy=LogRetentionPolicy(
                    event_days=30,
                    trace_days=30,
                    queue_days=14,
                    service_run_days=90,
                    mirror_max_bytes=16,
                    mirror_backups=2,
                ),
                now=datetime(2026, 4, 2, tzinfo=timezone.utc),
            )

            self.assertGreaterEqual(summary["deleted_events"], 1)
            self.assertGreaterEqual(summary["deleted_traces"], 1)
            self.assertGreaterEqual(summary["deleted_queue_rows"], 1)
            self.assertGreaterEqual(summary["deleted_service_runs"], 1)
            self.assertEqual(summary["rotated_terminal_log"], 1)
            self.assertEqual(summary["rotated_performance_log"], 1)
            self.assertTrue(paths.terminal_log.with_name("terminal.log.1").exists())
            self.assertTrue(paths.performance_log.with_name("performance.log.1").exists())

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

            with closing(sqlite3.connect(paths.database)) as connection:
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

            with closing(sqlite3.connect(paths.database)) as connection:
                row = connection.execute(
                    "SELECT queue_id, status, claimed_by_run_id FROM telegram_outbound_queue WHERE queue_id = 'claimed-live'"
                ).fetchone()

            self.assertEqual(row, ("claimed-live", "claimed", "live-run"))


if __name__ == "__main__":
    unittest.main()
