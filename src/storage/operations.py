from __future__ import annotations

import re
import socket
import uuid
from typing import Any

from app_meta import APP_VERSION
from core.models import utc_now
from core.paths import AppPaths
from core.process import process_exists

from .artifacts import ArtifactStore
from .db import StorageManager
from .payloads import GENERAL_PAYLOAD_LIMIT_BYTES, PREVIEW_LIMIT_BYTES, json_dumps, preview_text, truncate_utf8_bytes


class ServiceRunStore:
    def __init__(self, paths: AppPaths):
        self.paths = paths
        self.storage = StorageManager(paths)

    def start(self, *, run_id: str, pid: int | None = None) -> None:
        with self.storage.transaction() as connection:
            connection.execute(
                """
                INSERT INTO service_runs(run_id, started_at, version, pid, hostname, state_dir, exit_reason)
                VALUES (?, ?, ?, ?, ?, ?, NULL)
                ON CONFLICT(run_id) DO NOTHING
                """,
                (run_id, utc_now(), APP_VERSION, pid, socket.gethostname(), str(self.paths.root)),
            )
            stale_run_ids = {
                str(row["claimed_by_run_id"])
                for row in connection.execute(
                    """
                    SELECT DISTINCT q.claimed_by_run_id
                    FROM telegram_outbound_queue q
                    WHERE q.status = 'claimed'
                      AND q.claimed_by_run_id IS NOT NULL
                      AND q.claimed_by_run_id != ?
                    """,
                    (run_id,),
                ).fetchall()
                if row["claimed_by_run_id"] is not None
            }
            dead_run_ids: set[str] = set()
            for stale_run_id in stale_run_ids:
                run_row = connection.execute(
                    "SELECT pid, stopped_at, exit_reason FROM service_runs WHERE run_id = ?",
                    (stale_run_id,),
                ).fetchone()
                if run_row is None:
                    dead_run_ids.add(stale_run_id)
                    continue
                if run_row["stopped_at"] is not None or run_row["exit_reason"] is not None:
                    dead_run_ids.add(stale_run_id)
                    continue
                stale_pid = run_row["pid"]
                if isinstance(stale_pid, int) and stale_pid > 0 and not process_exists(stale_pid):
                    dead_run_ids.add(stale_run_id)
            connection.execute(
                """
                UPDATE telegram_outbound_queue
                SET status = 'queued', claimed_by_run_id = NULL, claimed_at = NULL
                WHERE status = 'claimed' AND claimed_by_run_id IS NULL
                """
            )
            for stale_run_id in dead_run_ids:
                connection.execute(
                    """
                    UPDATE telegram_outbound_queue
                    SET status = 'queued', claimed_by_run_id = NULL, claimed_at = NULL
                    WHERE status = 'claimed' AND claimed_by_run_id = ?
                    """,
                    (stale_run_id,),
                )

    def stop(self, *, run_id: str, exit_reason: str) -> None:
        with self.storage.transaction() as connection:
            connection.execute(
                """
                UPDATE service_runs
                SET stopped_at = ?, exit_reason = ?
                WHERE run_id = ?
                """,
                (utc_now(), exit_reason, run_id),
            )


class TraceStore:
    def __init__(self, paths: AppPaths, *, run_id: str | None = None):
        self.paths = paths
        self.storage = StorageManager(paths)
        self.artifacts = ArtifactStore(paths)
        self.run_id = run_id

    def start_trace(
        self,
        *,
        session_id: str | None,
        chat_id: int | None,
        topic_id: int | None,
        user_text: str,
        thread_id: str | None = None,
        turn_id: str | None = None,
        parent_trace_id: str | None = None,
    ) -> str:
        trace_id = str(uuid.uuid4())
        with self.storage.transaction() as connection:
            connection.execute(
                """
                INSERT INTO traces(
                    trace_id, session_id, thread_id, turn_id, parent_trace_id, chat_id, topic_id,
                    user_text_preview, started_at, completed_at, outcome
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)
                """,
                (
                    trace_id,
                    session_id,
                    thread_id,
                    turn_id,
                    parent_trace_id,
                    chat_id,
                    topic_id,
                    preview_text(user_text),
                    utc_now(),
                ),
            )
        self.log_event(
            source="service",
            event_type="trace.started",
            trace_id=trace_id,
            session_id=session_id,
            thread_id=thread_id,
            turn_id=turn_id,
            chat_id=chat_id,
            topic_id=topic_id,
            payload={"preview": preview_text(user_text)},
        )
        return trace_id

    def update_trace(self, trace_id: str, **fields: Any) -> None:
        if not fields:
            return
        assignments = ", ".join(f"{key} = ?" for key in fields)
        values = list(fields.values()) + [trace_id]
        with self.storage.transaction() as connection:
            connection.execute(f"UPDATE traces SET {assignments} WHERE trace_id = ?", values)

    def complete_trace(self, trace_id: str, *, outcome: str, thread_id: str | None = None, turn_id: str | None = None) -> None:
        fields: dict[str, Any] = {"completed_at": utc_now(), "outcome": outcome}
        if thread_id is not None:
            fields["thread_id"] = thread_id
        if turn_id is not None:
            fields["turn_id"] = turn_id
        self.update_trace(trace_id, **fields)
        self.log_event(
            source="service",
            event_type=f"trace.{outcome}",
            trace_id=trace_id,
            thread_id=thread_id,
            turn_id=turn_id,
        )

    def log_event(
        self,
        *,
        source: str,
        event_type: str,
        trace_id: str | None = None,
        session_id: str | None = None,
        thread_id: str | None = None,
        turn_id: str | None = None,
        item_id: str | None = None,
        source_event_id: str | None = None,
        chat_id: int | None = None,
        topic_id: int | None = None,
        message_group_id: str | None = None,
        telegram_message_id: int | None = None,
        payload: Any | None = None,
        handled: bool = True,
    ) -> None:
        effective_run_id = self.run_id
        if effective_run_id is not None:
            with self.storage.read_connection() as connection:
                run_row = connection.execute("SELECT 1 FROM service_runs WHERE run_id = ?", (effective_run_id,)).fetchone()
            if run_row is None:
                effective_run_id = None
        if session_id is not None:
            with self.storage.read_connection() as connection:
                session_row = connection.execute("SELECT 1 FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
            if session_row is None:
                session_id = None
        if trace_id is not None:
            with self.storage.read_connection() as connection:
                trace_row = connection.execute("SELECT 1 FROM traces WHERE trace_id = ?", (trace_id,)).fetchone()
            if trace_row is None:
                trace_id = None
        payload_json: str | None = None
        payload_preview: str | None = None
        artifact_id: str | None = None
        created_artifact: dict[str, object] | None = None
        try:
            with self.storage.transaction() as connection:
                if payload is not None:
                    payload_json = json_dumps(payload)
                    if len(payload_json.encode("utf-8")) > GENERAL_PAYLOAD_LIMIT_BYTES:
                        payload_preview = truncate_utf8_bytes(payload_json, PREVIEW_LIMIT_BYTES)
                        created_artifact = self.artifacts.write_text(
                            kind=self._artifact_kind(source, event_type),
                            text=payload_json,
                            suffix=".json",
                            connection=connection,
                        )
                        artifact_id = str(created_artifact["artifact_id"])
                        payload_json = None
                    else:
                        payload_preview = truncate_utf8_bytes(payload_json, PREVIEW_LIMIT_BYTES)
                connection.execute(
                    """
                    INSERT INTO events(
                        trace_id, run_id, source, event_type, received_at, handled_at,
                        session_id, thread_id, turn_id, item_id, source_event_id, chat_id, topic_id,
                        message_group_id, telegram_message_id, payload_json, payload_preview, artifact_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        trace_id,
                        effective_run_id,
                        source,
                        event_type,
                        utc_now(),
                        utc_now() if handled else None,
                        session_id,
                        thread_id,
                        turn_id,
                        item_id,
                        source_event_id,
                        chat_id,
                        topic_id,
                        message_group_id,
                        telegram_message_id,
                        payload_json,
                        payload_preview,
                        artifact_id,
                    ),
                )
        except Exception:
            if created_artifact is not None:
                self.artifacts.delete(created_artifact)
            raise

    @staticmethod
    def _artifact_kind(source: str, event_type: str) -> str:
        return re.sub(r"[^a-zA-Z0-9._-]+", "_", f"event_{source}_{event_type}").strip("_") or "event_payload"
