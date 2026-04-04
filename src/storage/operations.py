from __future__ import annotations

import re
import sqlite3
import socket
import uuid
from typing import Any

from app_meta import APP_VERSION
from core.models import utc_now
from core.paths import AppPaths
from core.process import process_exists

from .artifacts import ArtifactStore
from .db import StorageManager
from .logging_health import clear_logging_degraded, mark_logging_degraded
from .payloads import GENERAL_PAYLOAD_LIMIT_BYTES, PREVIEW_LIMIT_BYTES, json_dumps, preview_text, truncate_utf8_bytes


class ServiceRunStore:
    def __init__(self, paths: AppPaths):
        self.paths = paths
        self.storage = StorageManager(paths)

    def start(self, *, run_id: str, pid: int | None = None) -> None:
        try:
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
        except sqlite3.Error as exc:
            mark_logging_degraded(self.paths, operation="service_run_start", error=str(exc))
            return

    def stop(self, *, run_id: str, exit_reason: str) -> None:
        try:
            with self.storage.transaction() as connection:
                connection.execute(
                    """
                    UPDATE service_runs
                    SET stopped_at = ?, exit_reason = ?
                    WHERE run_id = ?
                    """,
                    (utc_now(), exit_reason, run_id),
                )
        except sqlite3.Error as exc:
            mark_logging_degraded(self.paths, operation="service_run_stop", error=str(exc))
            return


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
        source_event_id: str | None = None,
    ) -> str:
        trace_id = str(uuid.uuid4())
        try:
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
        except sqlite3.Error as exc:
            mark_logging_degraded(
                self.paths,
                operation="start_trace",
                error=str(exc),
                source="service",
                event_type="trace.started",
            )
        self.log_event(
            source="service",
            event_type="trace.started",
            trace_id=trace_id,
            session_id=session_id,
            thread_id=thread_id,
            turn_id=turn_id,
            source_event_id=source_event_id,
            chat_id=chat_id,
            topic_id=topic_id,
            payload={"preview": preview_text(user_text), "source_event_id": source_event_id},
        )
        return trace_id

    def update_trace(self, trace_id: str, **fields: Any) -> None:
        if not fields:
            return
        assignments = ", ".join(f"{key} = ?" for key in fields)
        values = list(fields.values()) + [trace_id]
        try:
            with self.storage.transaction() as connection:
                connection.execute(f"UPDATE traces SET {assignments} WHERE trace_id = ?", values)
        except sqlite3.Error as exc:
            mark_logging_degraded(
                self.paths,
                operation="update_trace",
                error=str(exc),
                source="service",
                event_type="trace.updated",
            )
            return

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
        created_artifact: dict[str, object] | None = None
        try:
            effective_run_id = self.run_id
            payload_json: str | None = None
            payload_preview: str | None = None
            artifact_id: str | None = None
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
                    self._insert_event_with_fallback(
                        connection,
                        trace_id=trace_id,
                        run_id=effective_run_id,
                        source=source,
                        event_type=event_type,
                        handled=handled,
                        session_id=session_id,
                        thread_id=thread_id,
                        turn_id=turn_id,
                        item_id=item_id,
                        source_event_id=source_event_id,
                        chat_id=chat_id,
                        topic_id=topic_id,
                        message_group_id=message_group_id,
                        telegram_message_id=telegram_message_id,
                        payload_json=payload_json,
                        payload_preview=payload_preview,
                        artifact_id=artifact_id,
                    )
                    recovered = clear_logging_degraded(self.paths)
                    if recovered is not None:
                        self._insert_event_row(
                            connection,
                            run_id=effective_run_id,
                            source="service",
                            event_type="service.degraded",
                            payload_json=json_dumps(
                                {
                                    "reason": "logging_sqlite_unavailable",
                                    "degraded_at": recovered.get("degraded_at"),
                                    "error": recovered.get("error"),
                                    "operation": recovered.get("operation"),
                                    "source": recovered.get("source"),
                                    "event_type": recovered.get("event_type"),
                                }
                            ),
                        )
                        self._insert_event_row(
                            connection,
                            run_id=effective_run_id,
                            source="service",
                            event_type="service.recovered",
                            payload_json=json_dumps(
                                {
                                    "reason": "logging_sqlite_recovered",
                                    "degraded_at": recovered.get("degraded_at"),
                                    "error": recovered.get("error"),
                                    "operation": recovered.get("operation"),
                                }
                            ),
                        )
            except Exception:
                if created_artifact is not None:
                    self.artifacts.delete(created_artifact)
                raise
        except sqlite3.Error as exc:
            mark_logging_degraded(
                self.paths,
                operation="log_event",
                error=str(exc),
                source=source,
                event_type=event_type,
            )
            return

    @staticmethod
    def _insert_event_row(
        connection,
        *,
        trace_id: str | None = None,
        run_id: str | None,
        source: str,
        event_type: str,
        handled: bool = True,
        session_id: str | None = None,
        thread_id: str | None = None,
        turn_id: str | None = None,
        item_id: str | None = None,
        source_event_id: str | None = None,
        chat_id: int | None = None,
        topic_id: int | None = None,
        message_group_id: str | None = None,
        telegram_message_id: int | None = None,
        payload_json: str | None = None,
        payload_preview: str | None = None,
        artifact_id: str | None = None,
    ) -> None:
        now = utc_now()
        if payload_preview is None and payload_json is not None:
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
                run_id,
                source,
                event_type,
                now,
                now if handled else None,
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

    @classmethod
    def _insert_event_with_fallback(
        cls,
        connection,
        *,
        trace_id: str | None,
        run_id: str | None,
        source: str,
        event_type: str,
        handled: bool,
        session_id: str | None,
        thread_id: str | None,
        turn_id: str | None,
        item_id: str | None,
        source_event_id: str | None,
        chat_id: int | None,
        topic_id: int | None,
        message_group_id: str | None,
        telegram_message_id: int | None,
        payload_json: str | None,
        payload_preview: str | None,
        artifact_id: str | None,
    ) -> None:
        attempts = (
            {"trace_id": trace_id, "run_id": run_id, "session_id": session_id},
            {"trace_id": trace_id, "run_id": None, "session_id": session_id},
            {"trace_id": trace_id, "run_id": None, "session_id": None},
            {"trace_id": None, "run_id": None, "session_id": session_id},
            {"trace_id": None, "run_id": None, "session_id": None},
        )
        last_error: sqlite3.IntegrityError | None = None
        for refs in attempts:
            try:
                cls._insert_event_row(
                    connection,
                    trace_id=refs["trace_id"],
                    run_id=refs["run_id"],
                    source=source,
                    event_type=event_type,
                    handled=handled,
                    session_id=refs["session_id"],
                    thread_id=thread_id,
                    turn_id=turn_id,
                    item_id=item_id,
                    source_event_id=source_event_id,
                    chat_id=chat_id,
                    topic_id=topic_id,
                    message_group_id=message_group_id,
                    telegram_message_id=telegram_message_id,
                    payload_json=payload_json,
                    payload_preview=payload_preview,
                    artifact_id=artifact_id,
                )
                return
            except sqlite3.IntegrityError as exc:
                last_error = exc
        assert last_error is not None
        raise last_error

    @staticmethod
    def _artifact_kind(source: str, event_type: str) -> str:
        return re.sub(r"[^a-zA-Z0-9._-]+", "_", f"event_{source}_{event_type}").strip("_") or "event_payload"
