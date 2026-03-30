from __future__ import annotations

from dataclasses import dataclass, field
import sqlite3
from typing import Any

from core.models import utc_now
from core.paths import AppPaths
from storage.artifacts import ArtifactStore
from storage.db import StorageManager
from storage.payloads import GENERAL_PAYLOAD_LIMIT_BYTES, PREVIEW_LIMIT_BYTES, json_dumps, truncate_utf8_bytes


@dataclass
class TelegramUpdateStoreState:
    processed_update_ids: list[int] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"processed_update_ids": self.processed_update_ids}

    @classmethod
    def from_dict(cls, data: dict) -> "TelegramUpdateStoreState":
        return cls(processed_update_ids=[int(item) for item in data.get("processed_update_ids", [])])


class TelegramUpdateStore:
    def __init__(self, paths: AppPaths, *, max_entries: int = 1024):
        self.paths = paths
        self.max_entries = max_entries
        self.storage = StorageManager(paths)
        self.artifacts = ArtifactStore(paths)

    def load(self) -> TelegramUpdateStoreState:
        with self.storage.read_connection() as connection:
            rows = connection.execute(
                """
                SELECT update_id
                FROM telegram_updates
                WHERE status = 'processed'
                ORDER BY processed_at DESC, update_id DESC
                LIMIT ?
                """,
                (self.max_entries,),
            ).fetchall()
        return TelegramUpdateStoreState(processed_update_ids=[int(row["update_id"]) for row in reversed(rows)])

    def save(self, state: TelegramUpdateStoreState) -> None:
        with self.storage.transaction() as connection:
            connection.execute("DELETE FROM telegram_updates")
            now = utc_now()
            for update_id in state.processed_update_ids[-self.max_entries :]:
                connection.execute(
                    """
                    INSERT INTO telegram_updates(update_id, received_at, processed_at, status)
                    VALUES (?, ?, ?, 'processed')
                    """,
                    (int(update_id), now, now),
                )

    def has_processed(self, update_id: int) -> bool:
        with self.storage.read_connection() as connection:
            row = connection.execute(
                "SELECT 1 FROM telegram_updates WHERE update_id = ? AND status = 'processed'",
                (int(update_id),),
            ).fetchone()
        return row is not None

    def mark_processed(
        self,
        update_id: int,
        *,
        chat_id: int | None = None,
        topic_id: int | None = None,
        payload: dict[str, Any] | None = None,
    ) -> bool:
        now = utc_now()
        payload_preview: str | None = None
        artifact_ref: dict[str, object] | None = None
        if payload is not None:
            payload_json = json_dumps(payload)
            payload_preview = truncate_utf8_bytes(payload_json, PREVIEW_LIMIT_BYTES)
        try:
            with self.storage.transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO telegram_updates(
                        update_id, chat_id, topic_id, received_at, processed_at, status, payload_preview, artifact_id
                    ) VALUES (?, ?, ?, ?, ?, 'processed', ?, ?)
                    """,
                    (int(update_id), chat_id, topic_id, now, now, payload_preview, None),
                )
                if payload is not None and len(payload_json.encode("utf-8")) > GENERAL_PAYLOAD_LIMIT_BYTES:
                    artifact_ref = self.artifacts.write_text(
                        kind="telegram_update_payload",
                        text=payload_json,
                        suffix=".json",
                        connection=connection,
                    )
                    connection.execute(
                        "UPDATE telegram_updates SET artifact_id = ? WHERE update_id = ?",
                        (str(artifact_ref["artifact_id"]), int(update_id)),
                    )
                self._trim_old_entries(connection)
        except sqlite3.IntegrityError:
            if artifact_ref is not None:
                self.artifacts.delete(artifact_ref)
            return False
        except Exception:
            if artifact_ref is not None:
                self.artifacts.delete(artifact_ref)
            raise
        return True

    def _trim_old_entries(self, connection) -> None:
        connection.execute(
            """
            DELETE FROM telegram_updates
            WHERE update_id IN (
                SELECT update_id
                FROM telegram_updates
                WHERE status = 'processed'
                ORDER BY processed_at DESC, update_id DESC
                LIMIT -1 OFFSET ?
            )
            """,
            (self.max_entries,),
        )
