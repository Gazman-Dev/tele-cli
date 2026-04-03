from __future__ import annotations

import io
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from core.paths import build_paths
from logs_command import run_logs_command
from storage.db import StorageManager
from storage.operations import ServiceRunStore, TraceStore


class _Args:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class LogsCommandTests(unittest.TestCase):
    def test_recent_prints_latest_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            trace_store = TraceStore(paths)
            trace_store.log_event(source="service", event_type="service.recovery", payload={"message": "Recovered"})

            buffer = io.StringIO()
            with redirect_stdout(buffer):
                run_logs_command(paths, _Args(logs_target="recent", limit=10, source=None, event_type=None))

            output = buffer.getvalue()
            self.assertIn("service.recovery", output)
            self.assertIn("Recovered", output)

    def test_trace_prints_trace_timeline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            trace_store = TraceStore(paths)
            trace_id = trace_store.start_trace(session_id=None, chat_id=22, topic_id=7, user_text="hello")
            trace_store.log_event(source="service", event_type="ai.request.started", trace_id=trace_id)
            trace_store.complete_trace(trace_id, outcome="completed")

            buffer = io.StringIO()
            with redirect_stdout(buffer):
                run_logs_command(paths, _Args(logs_target="trace", trace_id=trace_id))

            output = buffer.getvalue()
            self.assertIn(trace_id, output)
            self.assertIn("ai.request.started", output)
            self.assertIn("trace.completed", output)

    def test_queue_prints_queue_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            storage = StorageManager(paths)
            ServiceRunStore(paths).start(run_id="run-1")
            with storage.transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO telegram_outbound_queue(
                        queue_id, created_at, available_at, status, op_type, chat_id, topic_id, session_id, trace_id,
                        message_group_id, telegram_message_id, dedupe_key, priority, disable_notification, payload_json,
                        attempt_count, last_error, claimed_by_run_id, claimed_at, completed_at
                    ) VALUES (?, ?, ?, 'failed', 'send_message', 22, 7, NULL, NULL, NULL, NULL, NULL, 100, 0, ?, 2, ?, NULL, NULL, NULL)
                    """,
                    ("queue-1", "2026-04-02T00:00:00+00:00", "2026-04-02T00:00:00+00:00", '{"text":"hi"}', "boom"),
                )

            buffer = io.StringIO()
            with redirect_stdout(buffer):
                run_logs_command(paths, _Args(logs_target="queue", limit=10, status="failed"))

            output = buffer.getvalue()
            self.assertIn("queue-1", output)
            self.assertIn("failed", output)
            self.assertIn("boom", output)

    def test_failures_filters_failure_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            trace_store = TraceStore(paths)
            trace_store.log_event(source="service", event_type="service.recovery", payload={"message": "Recovered from failure"})
            trace_store.log_event(source="performance", event_type="telegram_send_completed", payload={"duration_ms": 5})

            buffer = io.StringIO()
            with redirect_stdout(buffer):
                run_logs_command(paths, _Args(logs_target="failures", limit=10))

            output = buffer.getvalue()
            self.assertIn("service.recovery", output)
            self.assertNotIn("telegram_send_completed", output)


if __name__ == "__main__":
    unittest.main()
