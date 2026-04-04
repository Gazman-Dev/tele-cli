from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from core.models import utc_now
from core.paths import AppPaths
from integrations.telegram import TelegramClient, TelegramError, is_topic_closed_error

from .artifacts import ArtifactStore
from .db import StorageManager
from .operations import TraceStore
from .payloads import QUEUE_PAYLOAD_LIMIT_BYTES, json_dumps, json_loads
from .telegram_groups import update_message_chunk_id


_ACTIVE_MANAGER: "TelegramDeliveryManager | None" = None
_RATE_LIMIT_STATE_KEY = "telegram_delivery_backoff"
_MAX_RATE_LIMIT_BACKOFF_SECONDS = 3600
_INITIAL_RATE_LIMIT_BACKOFF_SECONDS = 2
_QUEUE_THROTTLE_SECONDS = 0.05
_LIVE_PROGRESS_ACTIVE_STATUSES = {"RUNNING_TURN", "INTERRUPTED", "RECOVERING_TURN"}


def install_delivery_manager(paths: AppPaths, telegram: TelegramClient, *, run_id: str) -> None:
    global _ACTIVE_MANAGER
    manager = TelegramDeliveryManager(paths, telegram, run_id=run_id)
    manager.start()
    _ACTIVE_MANAGER = manager


def uninstall_delivery_manager() -> None:
    global _ACTIVE_MANAGER
    if _ACTIVE_MANAGER is not None:
        _ACTIVE_MANAGER.stop()
    _ACTIVE_MANAGER = None


def active_delivery_manager() -> "TelegramDeliveryManager | None":
    return _ACTIVE_MANAGER


class TelegramDeliveryManager:
    def __init__(self, paths: AppPaths, telegram: TelegramClient, *, run_id: str):
        self.paths = paths
        self.telegram = telegram
        self.run_id = run_id
        self.storage = StorageManager(paths)
        self.artifacts = ArtifactStore(paths)
        self.traces = TraceStore(paths, run_id=run_id)
        self._wake_event = threading.Event()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run_loop, name="telegram-sqlite-queue", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        deadline = time.monotonic() + 0.25
        while time.monotonic() < deadline:
            processed = self.process_next()
            if processed is not None:
                continue
            if not self._has_pending_items():
                break
            self._wake_event.wait(0.01)
            self._wake_event.clear()
        self._stop_event.set()
        self._wake_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def enqueue_and_wait(
        self,
        *,
        op_type: str,
        payload: dict[str, Any],
        allow_paused_return: bool = False,
        **metadata: Any,
    ) -> Any:
        queue_id = self.enqueue(op_type=op_type, payload=payload, **metadata)
        return self._wait_for_completion(queue_id, allow_paused_return=allow_paused_return)

    def enqueue(
        self,
        *,
        op_type: str,
        payload: dict[str, Any],
        chat_id: int,
        topic_id: int | None = None,
        session_id: str | None = None,
        trace_id: str | None = None,
        message_group_id: str | None = None,
        telegram_message_id: int | None = None,
        dedupe_key: str | None = None,
        priority: int = 100,
        disable_notification: bool = False,
    ) -> str:
        queue_id = str(uuid.uuid4())
        payload_json = json_dumps(payload)
        artifact_ref: dict[str, object] | None = None
        try:
            with self.storage.transaction() as connection:
                paused_until = self._load_paused_until(connection)
                created_at = utc_now()
                available_at = self._initial_available_at(paused_until)
                if len(payload_json.encode("utf-8")) > QUEUE_PAYLOAD_LIMIT_BYTES:
                    artifact_ref = self.artifacts.write_text(
                        kind="telegram_queue_payload",
                        text=payload_json,
                        suffix=".json",
                        connection=connection,
                    )
                    payload_json = json_dumps({"artifact": artifact_ref})
                if dedupe_key:
                    connection.execute(
                        """
                        DELETE FROM telegram_outbound_queue
                        WHERE dedupe_key = ? AND status = 'queued'
                        """,
                        (dedupe_key,),
                    )
                connection.execute(
                    """
                    INSERT INTO telegram_outbound_queue(
                        queue_id, created_at, available_at, status, op_type, chat_id, topic_id, session_id, trace_id,
                        message_group_id, telegram_message_id, dedupe_key, priority, disable_notification, payload_json,
                        attempt_count, last_error, claimed_by_run_id, claimed_at, completed_at
                    ) VALUES (?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, NULL, NULL, NULL)
                    """,
                    (
                        queue_id,
                        created_at,
                        available_at,
                        op_type,
                        chat_id,
                        topic_id,
                        session_id,
                        trace_id,
                        message_group_id,
                        telegram_message_id,
                        dedupe_key,
                        priority,
                        1 if disable_notification else 0,
                        payload_json,
                    ),
                )
        except Exception:
            if artifact_ref is not None:
                self.artifacts.delete(artifact_ref)
            raise
        self.traces.log_event(
            source="telegram_outbound",
            event_type="telegram.queue.enqueued",
            trace_id=trace_id,
            session_id=session_id,
            chat_id=chat_id,
            topic_id=topic_id,
            message_group_id=message_group_id,
            telegram_message_id=telegram_message_id,
            payload={"queue_id": queue_id, "op_type": op_type},
        )
        self._wake_event.set()
        return queue_id

    def _decode_payload(self, payload_json: str) -> dict[str, Any]:
        payload = json_loads(payload_json, {})
        if ArtifactStore.is_reference(payload):
            resolved = self.artifacts.read_json(payload, {})
            return resolved if isinstance(resolved, dict) else {}
        if isinstance(payload, dict) and ArtifactStore.is_reference(payload.get("artifact")):
            resolved = self.artifacts.read_json(payload["artifact"], {})
            return resolved if isinstance(resolved, dict) else {}
        return payload if isinstance(payload, dict) else {}

    def _encode_payload(self, connection: sqlite3.Connection, payload: dict[str, Any]) -> str:
        payload_json = json_dumps(payload)
        if len(payload_json.encode("utf-8")) <= QUEUE_PAYLOAD_LIMIT_BYTES:
            return payload_json
        artifact_ref = self.artifacts.write_text(
            kind="telegram_queue_payload",
            text=payload_json,
            suffix=".json",
            connection=connection,
        )
        return json_dumps({"artifact": artifact_ref})

    def _promote_pending_send_replacements(
        self,
        connection: sqlite3.Connection,
        *,
        current_queue_id: str,
        dedupe_key: str | None,
        telegram_message_id: int,
    ) -> None:
        if not dedupe_key:
            return
        rows = connection.execute(
            """
            SELECT queue_id, op_type, payload_json
            FROM telegram_outbound_queue
            WHERE dedupe_key = ?
              AND queue_id != ?
              AND status = 'queued'
            ORDER BY created_at ASC
            """,
            (dedupe_key, current_queue_id),
        ).fetchall()
        for queued_row in rows:
            if str(queued_row["op_type"]) != "send_message":
                continue
            payload = self._decode_payload(str(queued_row["payload_json"]))
            text = str(payload.get("text") or "")
            parse_mode = payload.get("parse_mode")
            updated_payload = {
                "message_id": telegram_message_id,
                "text": text,
                "parse_mode": parse_mode,
            }
            connection.execute(
                """
                UPDATE telegram_outbound_queue
                SET op_type = 'edit_message',
                    telegram_message_id = ?,
                    payload_json = ?
                WHERE queue_id = ?
                """,
                (
                    telegram_message_id,
                    self._encode_payload(connection, updated_payload),
                    str(queued_row["queue_id"]),
                ),
            )

    def _initial_available_at(self, paused_until: str | None) -> str:
        now = datetime.now(timezone.utc)
        available_at = now + timedelta(seconds=_QUEUE_THROTTLE_SECONDS)
        paused_at = self._parse_timestamp(paused_until)
        if paused_at is not None and paused_at > available_at:
            available_at = paused_at
        return available_at.isoformat()

    def _wait_for_completion(self, target_queue_id: str, *, allow_paused_return: bool = False) -> Any:
        deadline = time.monotonic() + float(_MAX_RATE_LIMIT_BACKOFF_SECONDS)
        while True:
            with self.storage.read_connection() as connection:
                row = connection.execute(
                    """
                    SELECT status, last_error, telegram_message_id, op_type, available_at
                    FROM telegram_outbound_queue
                    WHERE queue_id = ?
                    """,
                    (target_queue_id,),
                ).fetchone()
            if row is None:
                raise RuntimeError(f"Queue operation {target_queue_id} disappeared.")
            if row["status"] == "completed":
                if row["op_type"] in {"send_message", "send_photo", "send_document"} and row["telegram_message_id"] is not None:
                    return {"message_id": int(row["telegram_message_id"])}
                return None
            if row["status"] == "failed":
                raise RuntimeError(str(row["last_error"] or "Telegram queue operation failed."))
            if row["status"] == "queued":
                available_at = self._parse_timestamp(row["available_at"])
                queued_due_to_rate_limit = TelegramClient._retry_delay_from_error_text(str(row["last_error"] or "")) is not None
                if (
                    available_at is not None
                    and available_at > datetime.now(timezone.utc)
                    and (allow_paused_return or queued_due_to_rate_limit)
                ):
                    return {"queue_id": target_queue_id, "status": "queued"}
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Timed out waiting for Telegram queue item {target_queue_id}.")
            self._wake_event.wait(0.05)
            self._wake_event.clear()

    def _has_pending_items(self) -> bool:
        with self.storage.read_connection() as connection:
            row = connection.execute(
                """
                SELECT 1
                FROM telegram_outbound_queue
                WHERE status = 'queued'
                LIMIT 1
                """
            ).fetchone()
        return row is not None

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                processed = self.process_next()
            except sqlite3.Error:
                if self._stop_event.is_set():
                    break
                self._wake_event.wait(0.25)
                self._wake_event.clear()
                continue
            if processed is None:
                self._wake_event.wait(0.25)
                self._wake_event.clear()
                continue

    def process_next(self, *, target_queue_id: str | None = None) -> dict[str, Any] | None:
        del target_queue_id
        with self.storage.transaction() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM telegram_outbound_queue
                WHERE status = 'queued' AND available_at <= ?
                ORDER BY priority ASC, created_at ASC
                LIMIT 1
                """,
                (utc_now(),),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                """
                UPDATE telegram_outbound_queue
                SET status = 'claimed', claimed_by_run_id = ?, claimed_at = ?
                WHERE queue_id = ?
                """,
                (self.run_id, utc_now(), row["queue_id"]),
            )
        self.traces.log_event(
            source="telegram_outbound",
            event_type="telegram.queue.claimed",
            trace_id=row["trace_id"],
            session_id=row["session_id"],
            chat_id=row["chat_id"],
            topic_id=row["topic_id"],
            message_group_id=row["message_group_id"],
            telegram_message_id=row["telegram_message_id"],
            payload={"queue_id": row["queue_id"], "op_type": row["op_type"]},
        )
        stale_reason = self._skip_claimed_row_if_stale(row)
        if stale_reason is not None:
            self._wake_event.set()
            return {
                "queue_id": row["queue_id"],
                "status": "completed",
                "result": None,
                "error": None,
                "skipped": True,
                "skip_reason": stale_reason,
            }
        result: Any = None
        error_text: str | None = None
        final_status = "completed"
        try:
            result = self._execute_row(row)
        except Exception as exc:
            retry_after = TelegramClient._retry_delay_from_error(exc)
            with self.storage.transaction() as connection:
                current = connection.execute(
                    "SELECT attempt_count FROM telegram_outbound_queue WHERE queue_id = ?",
                    (row["queue_id"],),
                ).fetchone()
                attempt_count = int(current["attempt_count"]) if current is not None else 0
                next_attempt = attempt_count + 1
                error_text = str(exc)
                if retry_after is not None:
                    pause_until, backoff_seconds = self._advance_global_pause(
                        connection,
                        retry_after=retry_after,
                    )
                    final_status = "queued"
                    connection.execute(
                        """
                        UPDATE telegram_outbound_queue
                        SET status = 'queued',
                            attempt_count = ?,
                            last_error = ?,
                            available_at = ?,
                            claimed_by_run_id = NULL,
                            claimed_at = NULL
                        WHERE queue_id = ?
                        """,
                        (next_attempt, error_text, pause_until, row["queue_id"]),
                    )
                    connection.execute(
                        """
                        UPDATE telegram_outbound_queue
                        SET available_at = CASE
                            WHEN available_at < ? THEN ?
                            ELSE available_at
                        END
                        WHERE status = 'queued'
                        """,
                        (pause_until, pause_until),
                    )
                else:
                    final_status = "failed"
                    connection.execute(
                        """
                        UPDATE telegram_outbound_queue
                        SET status = 'failed',
                            attempt_count = ?,
                            last_error = ?,
                            completed_at = ?,
                            claimed_by_run_id = NULL
                        WHERE queue_id = ?
                        """,
                        (next_attempt, error_text, utc_now(), row["queue_id"]),
                    )
        else:
            with self.storage.transaction() as connection:
                message_id = None
                if isinstance(result, dict):
                    raw_message_id = result.get("message_id")
                    if isinstance(raw_message_id, int):
                        message_id = raw_message_id
                connection.execute(
                    """
                    UPDATE telegram_outbound_queue
                    SET status = 'completed',
                        completed_at = ?,
                        last_error = NULL,
                        telegram_message_id = COALESCE(?, telegram_message_id)
                    WHERE queue_id = ?
                    """,
                    (utc_now(), message_id, row["queue_id"]),
                )
                if message_id is not None and str(row["op_type"]) == "send_message":
                    self._promote_pending_send_replacements(
                        connection,
                        current_queue_id=str(row["queue_id"]),
                        dedupe_key=str(row["dedupe_key"]) if row["dedupe_key"] is not None else None,
                        telegram_message_id=message_id,
                    )
                self._clear_global_pause(connection)
            if (
                message_id is not None
                and isinstance(row["message_group_id"], str)
                and row["message_group_id"]
                and row["op_type"] in {"send_message", "send_photo", "send_document"}
            ):
                chunk_index = self._chunk_index_from_dedupe_key(row["dedupe_key"])
                if chunk_index is not None:
                    update_message_chunk_id(
                        self.paths,
                        message_group_id=str(row["message_group_id"]),
                        chunk_index=chunk_index,
                        telegram_message_id=message_id,
                    )
            self.traces.log_event(
                source="telegram_outbound",
                event_type=self._api_event_type(row["op_type"], success=True),
                trace_id=row["trace_id"],
                session_id=row["session_id"],
                chat_id=row["chat_id"],
                topic_id=row["topic_id"],
                message_group_id=row["message_group_id"],
                telegram_message_id=message_id if message_id is not None else row["telegram_message_id"],
                payload={"queue_id": row["queue_id"], "op_type": row["op_type"]},
            )
        if final_status == "queued" and error_text is not None:
            with self.storage.read_connection() as connection:
                paused_until = self._load_paused_until(connection)
                backoff_seconds = self._load_backoff_seconds(connection)
            self.traces.log_event(
                source="telegram_outbound",
                event_type="telegram.queue.rate_limited",
                trace_id=row["trace_id"],
                session_id=row["session_id"],
                chat_id=row["chat_id"],
                topic_id=row["topic_id"],
                message_group_id=row["message_group_id"],
                telegram_message_id=row["telegram_message_id"],
                payload={
                    "queue_id": row["queue_id"],
                    "op_type": row["op_type"],
                    "error": error_text,
                    "pause_until": paused_until,
                    "backoff_seconds": backoff_seconds,
                },
            )
        if final_status != "completed":
            self.traces.log_event(
                source="telegram_outbound",
                event_type=self._api_event_type(row["op_type"], success=False),
                trace_id=row["trace_id"],
                session_id=row["session_id"],
                chat_id=row["chat_id"],
                topic_id=row["topic_id"],
                message_group_id=row["message_group_id"],
                telegram_message_id=row["telegram_message_id"],
                payload={"queue_id": row["queue_id"], "op_type": row["op_type"], "error": error_text},
            )
        self.traces.log_event(
            source="telegram_outbound",
            event_type=f"telegram.queue.{final_status}",
            trace_id=row["trace_id"],
            session_id=row["session_id"],
            chat_id=row["chat_id"],
            topic_id=row["topic_id"],
            message_group_id=row["message_group_id"],
            telegram_message_id=row["telegram_message_id"],
            payload={"queue_id": row["queue_id"], "op_type": row["op_type"], "error": error_text},
        )
        self._wake_event.set()
        return {
            "queue_id": row["queue_id"],
            "status": final_status,
            "result": result,
            "error": error_text,
        }

    def _skip_claimed_row_if_stale(self, row) -> str | None:
        reason = self._stale_row_reason(row)
        if reason is None:
            return None
        with self.storage.transaction() as connection:
            connection.execute(
                """
                UPDATE telegram_outbound_queue
                SET status = 'completed',
                    completed_at = ?,
                    last_error = NULL,
                    claimed_by_run_id = NULL
                WHERE queue_id = ?
                """,
                (utc_now(), row["queue_id"]),
            )
        self.traces.log_event(
            source="telegram_outbound",
            event_type="telegram.queue.skipped_stale",
            trace_id=row["trace_id"],
            session_id=row["session_id"],
            chat_id=row["chat_id"],
            topic_id=row["topic_id"],
            message_group_id=row["message_group_id"],
            telegram_message_id=row["telegram_message_id"],
            payload={
                "queue_id": row["queue_id"],
                "op_type": row["op_type"],
                "reason": reason,
            },
        )
        return reason

    def _stale_row_reason(self, row) -> str | None:
        session_id = row["session_id"]
        if not isinstance(session_id, str) or not session_id:
            return None
        op_type = str(row["op_type"] or "")
        message_group_id = str(row["message_group_id"] or "")
        if op_type != "typing" and not message_group_id.startswith(f"{session_id}:live_progress:"):
            return None
        with self.storage.read_connection() as connection:
            session_row = connection.execute(
                """
                SELECT attached, status, active_turn_id, last_completed_turn_id, current_trace_id, last_user_message_at
                FROM sessions
                WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
        if session_row is None:
            return "session_missing"
        if not bool(session_row["attached"]):
            return "session_detached"
        if op_type == "typing":
            if str(session_row["status"] or "") not in _LIVE_PROGRESS_ACTIVE_STATUSES:
                return f"session_status:{session_row['status']}"
            active_turn_id = session_row["active_turn_id"]
            if not isinstance(active_turn_id, str) or not active_turn_id:
                return "no_active_turn"
            current_trace_id = session_row["current_trace_id"]
            if isinstance(row["trace_id"], str) and row["trace_id"]:
                if not isinstance(current_trace_id, str) or current_trace_id != row["trace_id"]:
                    return "trace_advanced"
            return None
        if str(session_row["status"] or "") not in _LIVE_PROGRESS_ACTIVE_STATUSES:
            return f"session_status:{session_row['status']}"
        expected_group_id = self._live_progress_group_id(session_id, session_row)
        if message_group_id != expected_group_id:
            return "group_replaced"
        return None

    @staticmethod
    def _live_progress_group_id(session_id: str, session_row) -> str:
        base = (
            session_row["current_trace_id"]
            or session_row["active_turn_id"]
            or session_row["last_completed_turn_id"]
            or "session"
        )
        timestamp = str(session_row["last_user_message_at"] or "").strip()
        trace_token = f"{base}:{timestamp}" if timestamp else str(base)
        return f"{session_id}:live_progress:{trace_token}"

    def is_paused(self) -> bool:
        with self.storage.read_connection() as connection:
            paused_until = self._load_paused_until(connection)
        until = self._parse_timestamp(paused_until)
        return until is not None and until > datetime.now(timezone.utc)

    def latest_message_id_for_dedupe(self, dedupe_key: str) -> int | None:
        if not dedupe_key:
            return None
        with self.storage.read_connection() as connection:
            row = connection.execute(
                """
                SELECT telegram_message_id
                FROM telegram_outbound_queue
                WHERE dedupe_key = ?
                  AND status = 'completed'
                  AND telegram_message_id IS NOT NULL
                ORDER BY completed_at DESC, created_at DESC
                LIMIT 1
                """,
                (dedupe_key,),
            ).fetchone()
        if row is None:
            return None
        message_id = row["telegram_message_id"]
        return int(message_id) if isinstance(message_id, int) else None

    @staticmethod
    def _chunk_index_from_dedupe_key(dedupe_key: str | None) -> int | None:
        if not isinstance(dedupe_key, str) or ":chunk:" not in dedupe_key:
            return None
        try:
            return int(dedupe_key.rsplit(":chunk:", 1)[1])
        except ValueError:
            return None

    @staticmethod
    def _api_event_type(op_type: str, *, success: bool) -> str:
        if not success:
            return "telegram.api.failed"
        mapping = {
            "send_message": "telegram.api.send.completed",
            "edit_message": "telegram.api.edit.completed",
            "delete_message": "telegram.api.delete.completed",
            "typing": "telegram.api.typing.completed",
            "send_photo": "telegram.api.send_photo.completed",
            "send_document": "telegram.api.send_document.completed",
        }
        return mapping.get(op_type, "telegram.api.completed")

    def _execute_row(self, row) -> Any:
        payload = json_loads(row["payload_json"], {})
        if ArtifactStore.is_reference(payload):
            resolved = self.artifacts.read_json(payload, {})
            payload = resolved if isinstance(resolved, dict) else {}
        elif isinstance(payload, dict) and ArtifactStore.is_reference(payload.get("artifact")):
            resolved = self.artifacts.read_json(payload["artifact"], {})
            payload = resolved if isinstance(resolved, dict) else {}
        op_type = row["op_type"]
        if op_type == "send_message":
            return self._invoke_topic_aware_telegram(
                self.telegram.send_message,
                int(row["chat_id"]),
                str(payload["text"]),
                topic_id=row["topic_id"],
                parse_mode=payload.get("parse_mode"),
                disable_notification=bool(row["disable_notification"]),
            )
        if op_type == "edit_message":
            return self._invoke_telegram(
                self.telegram.edit_message_text,
                int(row["chat_id"]),
                int(payload["message_id"]),
                str(payload["text"]),
                parse_mode=payload.get("parse_mode"),
            )
        if op_type == "delete_message":
            return self.telegram.delete_message(int(row["chat_id"]), int(payload["message_id"]))
        if op_type == "typing":
            return self._invoke_topic_aware_telegram(self.telegram.send_typing, int(row["chat_id"]), topic_id=row["topic_id"])
        if op_type == "send_photo":
            return self._invoke_topic_aware_telegram(
                self.telegram.send_photo,
                int(row["chat_id"]),
                Path(str(payload["photo_path"])),
                topic_id=row["topic_id"],
                caption=payload.get("caption"),
                parse_mode=payload.get("parse_mode"),
                disable_notification=bool(row["disable_notification"]),
            )
        if op_type == "send_document":
            return self._invoke_topic_aware_telegram(
                self.telegram.send_document,
                int(row["chat_id"]),
                Path(str(payload["document_path"])),
                topic_id=row["topic_id"],
                caption=payload.get("caption"),
                parse_mode=payload.get("parse_mode"),
                disable_notification=bool(row["disable_notification"]),
            )
        raise RuntimeError(f"Unsupported Telegram queue operation {op_type}.")

    @staticmethod
    def _invoke_telegram(func, *args, **kwargs) -> Any:
        try:
            return func(*args, **kwargs)
        except TypeError:
            reduced = dict(kwargs)
            for key in ("disable_notification", "parse_mode", "caption", "topic_id"):
                reduced.pop(key, None)
                try:
                    return func(*args, **reduced)
                except TypeError:
                    continue
            raise

    @classmethod
    def _invoke_topic_aware_telegram(cls, func, *args, **kwargs) -> Any:
        topic_id = kwargs.get("topic_id")
        try:
            return cls._invoke_telegram(func, *args, **kwargs)
        except TelegramError as exc:
            if topic_id is None or not is_topic_closed_error(exc):
                raise
            retry_kwargs = dict(kwargs)
            retry_kwargs["topic_id"] = None
            return cls._invoke_telegram(func, *args, **retry_kwargs)

    @staticmethod
    def _parse_timestamp(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def _load_rate_limit_state(self, connection: sqlite3.Connection) -> dict[str, Any]:
        row = connection.execute(
            "SELECT value_json FROM app_state WHERE state_key = ?",
            (_RATE_LIMIT_STATE_KEY,),
        ).fetchone()
        if row is None:
            return {}
        return json_loads(row["value_json"], {})

    def _load_paused_until(self, connection: sqlite3.Connection) -> str | None:
        state = self._load_rate_limit_state(connection)
        paused_until = state.get("paused_until")
        return str(paused_until) if isinstance(paused_until, str) and paused_until else None

    def _load_backoff_seconds(self, connection: sqlite3.Connection) -> int:
        state = self._load_rate_limit_state(connection)
        raw = state.get("backoff_seconds")
        return int(raw) if isinstance(raw, (int, float)) and raw > 0 else 0

    def _save_rate_limit_state(
        self,
        connection: sqlite3.Connection,
        *,
        paused_until: str | None,
        backoff_seconds: int,
    ) -> None:
        connection.execute(
            """
            INSERT INTO app_state(state_key, value_json, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(state_key) DO UPDATE SET
                value_json = excluded.value_json,
                updated_at = excluded.updated_at
            """,
            (
                _RATE_LIMIT_STATE_KEY,
                json_dumps(
                    {
                        "paused_until": paused_until,
                        "backoff_seconds": backoff_seconds,
                    }
                ),
                utc_now(),
            ),
        )

    def _advance_global_pause(self, connection: sqlite3.Connection, *, retry_after: float | None) -> tuple[str, int]:
        current_backoff = self._load_backoff_seconds(connection)
        next_backoff = (
            _INITIAL_RATE_LIMIT_BACKOFF_SECONDS
            if current_backoff < _INITIAL_RATE_LIMIT_BACKOFF_SECONDS
            else min(current_backoff * 2, _MAX_RATE_LIMIT_BACKOFF_SECONDS)
        )
        if retry_after is not None:
            next_backoff = max(next_backoff, max(int(retry_after), 1))
        next_backoff = min(next_backoff, _MAX_RATE_LIMIT_BACKOFF_SECONDS)
        paused_until = (datetime.now(timezone.utc) + timedelta(seconds=next_backoff)).isoformat()
        self._save_rate_limit_state(
            connection,
            paused_until=paused_until,
            backoff_seconds=next_backoff,
        )
        return paused_until, next_backoff

    def _clear_global_pause(self, connection: sqlite3.Connection) -> None:
        if not self._load_rate_limit_state(connection):
            return
        self._save_rate_limit_state(connection, paused_until=None, backoff_seconds=0)
