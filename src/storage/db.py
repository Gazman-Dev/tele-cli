from __future__ import annotations

from contextlib import contextmanager
from dataclasses import fields
import hashlib
import json
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from core.models import CodexServerState, RuntimeState, SessionRecord
from core.paths import AppPaths
from runtime.instructions import session_short_memory_relpath

from .payloads import GENERAL_PAYLOAD_LIMIT_BYTES, json_dumps, preview_text


_MIGRATION_GUARD = threading.RLock()
_INITIALIZED_DATABASES: set[Path] = set()
_BOOTSTRAP_STATE_KEY = "sqlite_bootstrap"
_SQLITE_CONNECT_TIMEOUT_SECONDS = 30.0
_SQLITE_BUSY_TIMEOUT_MS = 30000
_SQLITE_LOCK_RETRY_SECONDS = (0.05, 0.1, 0.2, 0.5, 1.0)
_REQUIRED_TABLES = (
    "service_runs",
    "app_state",
    "sessions",
    "session_short_memory",
    "workspaces",
    "telegram_updates",
    "traces",
    "approvals",
    "events",
    "telegram_outbound_queue",
    "telegram_message_groups",
    "telegram_message_chunks",
    "artifacts",
)


def utc_now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _migration_dir() -> Path:
    return Path(__file__).resolve().parent / "migrations"


def _migration_files() -> list[tuple[int, str, Path]]:
    files: list[tuple[int, str, Path]] = []
    for path in sorted(_migration_dir().glob("*.sql")):
        prefix = path.stem.split("_", 1)[0]
        try:
            version = int(prefix)
        except ValueError as exc:
            raise RuntimeError(f"Invalid migration filename {path.name!r}.") from exc
        files.append((version, path.name, path))
    return files


def _checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _configure_connection(connection: sqlite3.Connection) -> None:
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = NORMAL")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute(f"PRAGMA busy_timeout = {_SQLITE_BUSY_TIMEOUT_MS}")


def _is_sqlite_lock_error(exc: sqlite3.Error) -> bool:
    message = str(exc).lower()
    return "database is locked" in message or "database table is locked" in message


def _connect_with_retry(database: Path) -> sqlite3.Connection:
    last_error: sqlite3.Error | None = None
    for delay_seconds in (0.0, *_SQLITE_LOCK_RETRY_SECONDS):
        if delay_seconds > 0:
            time.sleep(delay_seconds)
        try:
            connection = sqlite3.connect(database, timeout=_SQLITE_CONNECT_TIMEOUT_SECONDS)
            _configure_connection(connection)
            return connection
        except sqlite3.Error as exc:
            last_error = exc
            if not _is_sqlite_lock_error(exc):
                raise
    assert last_error is not None
    raise last_error


def _legacy_json_path(paths: AppPaths, filename: str) -> Path:
    return paths.root / filename


def _load_legacy_data(paths: AppPaths, filename: str) -> dict | None:
    path = _legacy_json_path(paths, filename)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _write_bootstrap_artifact_text(
    connection: sqlite3.Connection,
    paths: AppPaths,
    *,
    kind: str,
    text: str,
    suffix: str,
) -> dict[str, object]:
    artifact_id = str(uuid.uuid4())
    directory = paths.artifacts / kind
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{artifact_id}{suffix}"
    path.write_text(text, encoding="utf-8")
    payload = path.read_bytes()
    relpath = path.relative_to(paths.root).as_posix()
    connection.execute(
        """
        INSERT INTO artifacts(artifact_id, kind, relpath, size_bytes, sha256, created_at, expires_at, compressed)
        VALUES (?, ?, ?, ?, ?, ?, NULL, 0)
        """,
        (artifact_id, kind, relpath, len(payload), hashlib.sha256(payload).hexdigest(), utc_now()),
    )
    return {
        "storage": "artifact",
        "artifact_id": artifact_id,
        "kind": kind,
        "relpath": relpath,
        "size_bytes": len(payload),
        "preview": preview_text(text),
    }


def _serialize_bootstrap_approval_params(connection: sqlite3.Connection, paths: AppPaths, params: dict) -> str:
    params_json = json_dumps(params)
    if len(params_json.encode("utf-8")) <= GENERAL_PAYLOAD_LIMIT_BYTES:
        return params_json
    artifact_ref = _write_bootstrap_artifact_text(
        connection,
        paths,
        kind="approval_params",
        text=params_json,
        suffix=".json",
    )
    return json_dumps(artifact_ref)


def _record_bootstrap_event(connection: sqlite3.Connection, *, event_type: str, payload: dict) -> None:
    payload_json = json_dumps(payload)
    connection.execute(
        """
        INSERT INTO events(
            trace_id, run_id, source, event_type, received_at, handled_at,
            session_id, thread_id, turn_id, item_id, source_event_id, chat_id, topic_id,
            message_group_id, telegram_message_id, payload_json, payload_preview, artifact_id
        ) VALUES (NULL, NULL, 'storage', ?, ?, ?, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, ?, ?, NULL)
        """,
        (event_type, utc_now(), utc_now(), payload_json, preview_text(payload_json)),
    )


def _has_bootstrap_marker(connection: sqlite3.Connection) -> bool:
    row = connection.execute("SELECT 1 FROM app_state WHERE state_key = ?", (_BOOTSTRAP_STATE_KEY,)).fetchone()
    return row is not None


def _database_has_runtime_rows(connection: sqlite3.Connection) -> bool:
    checks = (
        "SELECT 1 FROM sessions LIMIT 1",
        "SELECT 1 FROM approvals LIMIT 1",
        "SELECT 1 FROM telegram_updates LIMIT 1",
        "SELECT 1 FROM app_state WHERE state_key IN ('runtime', 'codex_server') LIMIT 1",
    )
    for query in checks:
        if connection.execute(query).fetchone() is not None:
            return True
    return False


def _has_required_schema(connection: sqlite3.Connection) -> bool:
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    tables = {str(row["name"]) for row in rows}
    return all(table in tables for table in _REQUIRED_TABLES)


def _import_legacy_session_rows(connection: sqlite3.Connection, paths: AppPaths, data: dict) -> int:
    imported = 0
    for item in data.get("sessions", []):
        session = SessionRecord.from_dict(item)
        connection.execute(
            """
            INSERT INTO sessions(
                session_id, transport, transport_user_id, transport_chat_id, transport_topic_id, transport_channel,
                attached, status, thread_id, active_turn_id, last_completed_turn_id, current_trace_id,
                instructions_dirty, last_seen_generation, created_at, last_user_message_at, last_agent_message_at,
                streaming_message_id, streaming_message_ids_json, thinking_message_id, thinking_message_ids_json,
                thinking_live_message_ids_json, thinking_live_texts_json, thinking_sent_texts_json,
                thinking_history_order_json, thinking_history_by_source_json, streaming_output_text, streaming_phase,
                thinking_message_text, thinking_history_text, last_thinking_sent_text, pending_output_text,
                queued_user_input_text, pending_output_updated_at, last_delivered_output_text
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            ON CONFLICT(session_id) DO NOTHING
            """,
            (
                session.session_id,
                session.transport,
                session.transport_user_id,
                session.transport_chat_id,
                session.transport_topic_id,
                session.transport_channel,
                1 if session.attached else 0,
                session.status,
                session.thread_id,
                session.active_turn_id,
                session.last_completed_turn_id,
                session.current_trace_id,
                1 if session.instructions_dirty else 0,
                int(session.last_seen_generation),
                session.created_at,
                session.last_user_message_at,
                session.last_agent_message_at,
                session.streaming_message_id,
                json_dumps(session.streaming_message_ids),
                session.thinking_message_id,
                json_dumps(session.thinking_message_ids),
                json_dumps(session.thinking_live_message_ids),
                json_dumps(session.thinking_live_texts),
                json_dumps(session.thinking_sent_texts),
                json_dumps(session.thinking_history_order),
                json_dumps(session.thinking_history_by_source),
                session.streaming_output_text,
                session.streaming_phase,
                session.thinking_message_text,
                session.thinking_history_text,
                session.last_thinking_sent_text,
                session.pending_output_text,
                session.queued_user_input_text,
                session.pending_output_updated_at,
                session.last_delivered_output_text,
            ),
        )
        connection.execute(
            """
            INSERT INTO session_short_memory(session_id, short_memory_relpath, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(session_id) DO NOTHING
            """,
            (session.session_id, session_short_memory_relpath(session.session_id), utc_now()),
        )
        imported += 1
    return imported


def _import_legacy_approval_rows(connection: sqlite3.Connection, paths: AppPaths, data: dict) -> int:
    imported = 0
    for item in data.get("approvals", []):
        request_id = int(item["request_id"])
        created_at = item.get("created_at") or utc_now()
        resolved_at = item.get("resolved_at")
        updated_at = item.get("updated_at") or resolved_at or created_at
        connection.execute(
            """
            INSERT INTO approvals(
                request_id, session_id, thread_id, turn_id, trace_id, method, params_json, status,
                created_at, updated_at, resolved_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(request_id) DO NOTHING
            """,
            (
                request_id,
                item.get("session_id"),
                item.get("thread_id"),
                item.get("turn_id"),
                item.get("trace_id"),
                item["method"],
                _serialize_bootstrap_approval_params(connection, paths, item.get("params", {})),
                item.get("status", "pending"),
                created_at,
                updated_at,
                resolved_at,
            ),
        )
        imported += 1
    return imported


def _import_legacy_update_rows(connection: sqlite3.Connection, data: dict) -> int:
    imported = 0
    now = utc_now()
    for raw_update_id in data.get("processed_update_ids", []):
        update_id = int(raw_update_id)
        connection.execute(
            """
            INSERT INTO telegram_updates(update_id, received_at, processed_at, status)
            VALUES (?, ?, ?, 'processed')
            ON CONFLICT(update_id) DO NOTHING
            """,
            (update_id, now, now),
        )
        imported += 1
    return imported


def _import_legacy_app_state(connection: sqlite3.Connection, data: dict, *, key: str) -> bool:
    model = RuntimeState if key == "runtime" else CodexServerState
    payload = data.get("payload") if isinstance(data.get("payload"), dict) else data
    allowed_keys = {field.name for field in fields(model)}
    normalized = {name: value for name, value in payload.items() if name in allowed_keys}
    value = model.from_dict(normalized).to_dict()
    connection.execute(
        """
        INSERT INTO app_state(state_key, value_json, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(state_key) DO NOTHING
        """,
        (key, json_dumps(value), utc_now()),
    )
    return True


def _bootstrap_legacy_state(connection: sqlite3.Connection, paths: AppPaths) -> None:
    if _has_bootstrap_marker(connection):
        return
    if _database_has_runtime_rows(connection):
        _record_bootstrap_event(
            connection,
            event_type="storage.bootstrap.preexisting_state",
            payload={"preexisting_state": True},
        )
        connection.execute(
            """
            INSERT INTO app_state(state_key, value_json, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(state_key) DO UPDATE SET
                value_json = excluded.value_json,
                updated_at = excluded.updated_at
            """,
            (
                _BOOTSTRAP_STATE_KEY,
                json_dumps({"completed_at": utc_now(), "imported_counts": {}, "imported_files": [], "preexisting_state": True}),
                utc_now(),
            ),
        )
        return
    imported_counts: dict[str, int] = {}
    imported_files: list[str] = []
    sessions_data = _load_legacy_data(paths, "sessions.json")
    if isinstance(sessions_data, dict):
        imported_counts["sessions"] = _import_legacy_session_rows(connection, paths, sessions_data)
        imported_files.append("sessions.json")
        _record_bootstrap_event(
            connection,
            event_type="storage.bootstrap.legacy_import",
            payload={"file": "sessions.json", "imported_count": imported_counts["sessions"]},
        )
    approvals_data = _load_legacy_data(paths, "approvals.json")
    if isinstance(approvals_data, dict):
        imported_counts["approvals"] = _import_legacy_approval_rows(connection, paths, approvals_data)
        imported_files.append("approvals.json")
        _record_bootstrap_event(
            connection,
            event_type="storage.bootstrap.legacy_import",
            payload={"file": "approvals.json", "imported_count": imported_counts["approvals"]},
        )
    updates_data = _load_legacy_data(paths, "telegram_updates.json")
    if isinstance(updates_data, dict):
        imported_counts["telegram_updates"] = _import_legacy_update_rows(connection, updates_data)
        imported_files.append("telegram_updates.json")
        _record_bootstrap_event(
            connection,
            event_type="storage.bootstrap.legacy_import",
            payload={"file": "telegram_updates.json", "imported_count": imported_counts["telegram_updates"]},
        )
    runtime_data = _load_legacy_data(paths, "runtime.json")
    if isinstance(runtime_data, dict):
        _import_legacy_app_state(connection, runtime_data, key="runtime")
        imported_counts["runtime"] = 1
        imported_files.append("runtime.json")
        _record_bootstrap_event(
            connection,
            event_type="storage.bootstrap.legacy_import",
            payload={"file": "runtime.json", "imported_count": 1},
        )
    codex_data = _load_legacy_data(paths, "codex_server.json")
    if isinstance(codex_data, dict):
        _import_legacy_app_state(connection, codex_data, key="codex_server")
        imported_counts["codex_server"] = 1
        imported_files.append("codex_server.json")
        _record_bootstrap_event(
            connection,
            event_type="storage.bootstrap.legacy_import",
            payload={"file": "codex_server.json", "imported_count": 1},
        )
    _record_bootstrap_event(
        connection,
        event_type="storage.bootstrap.completed",
        payload={"imported_counts": imported_counts, "imported_files": imported_files},
    )
    connection.execute(
        """
        INSERT INTO app_state(state_key, value_json, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(state_key) DO UPDATE SET
            value_json = excluded.value_json,
            updated_at = excluded.updated_at
        """,
        (
            _BOOTSTRAP_STATE_KEY,
            json_dumps(
                {
                    "completed_at": utc_now(),
                    "imported_counts": imported_counts,
                    "imported_files": imported_files,
                }
            ),
            utc_now(),
        ),
    )


class StorageManager:
    def __init__(self, paths: AppPaths):
        self.paths = paths
        ensure_storage(paths)

    def connect(self) -> sqlite3.Connection:
        self.paths.root.mkdir(parents=True, exist_ok=True)
        return _connect_with_retry(self.paths.database)

    @contextmanager
    def read_connection(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self, *, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        last_error: sqlite3.Error | None = None
        connection: sqlite3.Connection | None = None
        began = False
        try:
            for delay_seconds in (0.0, *_SQLITE_LOCK_RETRY_SECONDS):
                if delay_seconds > 0:
                    time.sleep(delay_seconds)
                connection = self.connect()
                try:
                    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
                    began = True
                    break
                except sqlite3.Error as exc:
                    connection.close()
                    connection = None
                    last_error = exc
                    if not _is_sqlite_lock_error(exc):
                        raise
            if not began:
                assert last_error is not None
                raise last_error
            yield connection
            connection.commit()
        except Exception:
            if connection is not None:
                connection.rollback()
            raise
        finally:
            if connection is not None:
                connection.close()


def ensure_storage(paths: AppPaths) -> None:
    database_path = paths.database.resolve()
    with _MIGRATION_GUARD:
        if database_path in _INITIALIZED_DATABASES and database_path.exists():
            return
        _INITIALIZED_DATABASES.discard(database_path)
        paths.root.mkdir(parents=True, exist_ok=True)
        connection = _connect_with_retry(paths.database)
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    id INTEGER PRIMARY KEY,
                    version INTEGER NOT NULL UNIQUE,
                    name TEXT NOT NULL UNIQUE,
                    checksum TEXT NOT NULL,
                    applied_at TEXT NOT NULL
                )
                """
            )
            applied = {
                int(row["version"]): (str(row["name"]), str(row["checksum"]))
                for row in connection.execute("SELECT version, name, checksum FROM schema_migrations ORDER BY version")
            }
            schema_needs_repair = not _has_required_schema(connection)
            for version, name, path in _migration_files():
                checksum = _checksum(path)
                existing = applied.get(version)
                if existing is not None:
                    existing_name, existing_checksum = existing
                    if existing_name != name or existing_checksum != checksum:
                        raise RuntimeError(f"Migration drift detected for version {version}: {name}.")
                    if not schema_needs_repair:
                        continue
                script = path.read_text(encoding="utf-8")
                connection.executescript(script)
                if existing is None:
                    connection.execute(
                        """
                        INSERT INTO schema_migrations(version, name, checksum, applied_at)
                        VALUES (?, ?, ?, ?)
                        """,
                        (version, name, checksum, utc_now()),
                    )
            if not _has_required_schema(connection):
                raise RuntimeError("Database schema is incomplete after applying migrations.")
            _bootstrap_legacy_state(connection, paths)
            connection.commit()
            _INITIALIZED_DATABASES.add(database_path)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
