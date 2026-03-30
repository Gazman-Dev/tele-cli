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
from integrations.telegram import TelegramClient

from .artifacts import ArtifactStore
from .db import StorageManager
from .operations import TraceStore
from .payloads import QUEUE_PAYLOAD_LIMIT_BYTES, json_dumps, json_loads


_ACTIVE_MANAGER: "TelegramDeliveryManager | None" = None


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
        self._stop_event.set()
        self._wake_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def enqueue_and_wait(self, *, op_type: str, payload: dict[str, Any], **metadata: Any) -> Any:
        queue_id = self.enqueue(op_type=op_type, payload=payload, **metadata)
        return self._wait_for_completion(queue_id)

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
                        utc_now(),
                        utc_now(),
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

    def _wait_for_completion(self, target_queue_id: str) -> Any:
        deadline = time.monotonic() + 60.0
        while True:
            with self.storage.read_connection() as connection:
                row = connection.execute(
                    "SELECT status, last_error, telegram_message_id, op_type FROM telegram_outbound_queue WHERE queue_id = ?",
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
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Timed out waiting for Telegram queue item {target_queue_id}.")
            self._wake_event.wait(0.05)
            self._wake_event.clear()

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
                if retry_after is not None and next_attempt <= 6:
                    final_status = "queued"
                    next_available = (datetime.now(timezone.utc) + timedelta(seconds=max(int(retry_after), 1))).isoformat()
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
                        (next_attempt, error_text, next_available, row["queue_id"]),
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
        return {
            "queue_id": row["queue_id"],
            "status": final_status,
            "result": result,
            "error": error_text,
        }

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
            return self._invoke_telegram(
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
            return self._invoke_telegram(self.telegram.send_typing, int(row["chat_id"]), topic_id=row["topic_id"])
        if op_type == "send_photo":
            return self._invoke_telegram(
                self.telegram.send_photo,
                int(row["chat_id"]),
                Path(str(payload["photo_path"])),
                topic_id=row["topic_id"],
                caption=payload.get("caption"),
                parse_mode=payload.get("parse_mode"),
                disable_notification=bool(row["disable_notification"]),
            )
        if op_type == "send_document":
            return self._invoke_telegram(
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
