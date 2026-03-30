from __future__ import annotations

from typing import Sequence

from core.models import utc_now
from core.paths import AppPaths

from .artifacts import ArtifactStore
from .db import StorageManager
from .payloads import CHUNK_PAYLOAD_LIMIT_BYTES


def upsert_message_group(
    paths: AppPaths,
    *,
    message_group_id: str,
    session_id: str | None,
    trace_id: str | None,
    chat_id: int,
    topic_id: int | None,
    logical_role: str,
    status: str,
    finalized: bool = False,
) -> None:
    storage = StorageManager(paths)
    now = utc_now()
    with storage.transaction() as connection:
        connection.execute(
            """
            INSERT INTO telegram_message_groups(
                message_group_id, session_id, trace_id, chat_id, topic_id, logical_role, status,
                created_at, updated_at, finalized_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(message_group_id) DO UPDATE SET
                session_id = excluded.session_id,
                trace_id = excluded.trace_id,
                chat_id = excluded.chat_id,
                topic_id = excluded.topic_id,
                logical_role = excluded.logical_role,
                status = excluded.status,
                updated_at = excluded.updated_at,
                finalized_at = excluded.finalized_at
            """,
            (
                message_group_id,
                session_id,
                trace_id,
                chat_id,
                topic_id,
                logical_role,
                status,
                now,
                now,
                now if finalized else None,
            ),
        )


def sync_message_chunks(
    paths: AppPaths,
    *,
    message_group_id: str,
    rendered_chunks: Sequence[str],
    telegram_message_ids: Sequence[int],
) -> None:
    storage = StorageManager(paths)
    artifacts = ArtifactStore(paths)
    now = utc_now()
    created_artifacts: list[dict[str, object]] = []
    try:
        with storage.transaction() as connection:
            chunk_rows: list[tuple[int, int | None, str, str | None]] = []
            for index, chunk in enumerate(rendered_chunks):
                artifact_id = None
                inline_html = chunk
                if len(chunk.encode("utf-8")) > CHUNK_PAYLOAD_LIMIT_BYTES:
                    artifact_ref = artifacts.write_text(
                        kind="telegram_chunk_html",
                        text=chunk,
                        suffix=".html",
                        connection=connection,
                    )
                    created_artifacts.append(artifact_ref)
                    artifact_id = str(artifact_ref["artifact_id"])
                    inline_html = chunk[:2048]
                telegram_message_id = telegram_message_ids[index] if index < len(telegram_message_ids) else None
                chunk_rows.append((index, telegram_message_id, inline_html, artifact_id))
            for index, telegram_message_id, inline_html, artifact_id in chunk_rows:
                connection.execute(
                    """
                    INSERT INTO telegram_message_chunks(
                        message_group_id, chunk_index, telegram_message_id, rendered_html, artifact_id,
                        created_at, updated_at, deleted_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
                    ON CONFLICT(message_group_id, chunk_index) DO UPDATE SET
                        telegram_message_id = excluded.telegram_message_id,
                        rendered_html = excluded.rendered_html,
                        artifact_id = excluded.artifact_id,
                        updated_at = excluded.updated_at,
                        deleted_at = NULL
                    """,
                    (message_group_id, index, telegram_message_id, inline_html, artifact_id, now, now),
                )
            connection.execute(
                """
                UPDATE telegram_message_chunks
                SET deleted_at = ?
                WHERE message_group_id = ? AND chunk_index >= ?
                """,
                (now, message_group_id, len(rendered_chunks)),
            )
    except Exception:
        for artifact_ref in created_artifacts:
            artifacts.delete(artifact_ref)
        raise
