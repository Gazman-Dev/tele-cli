from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from core.models import utc_now
from core.paths import AppPaths
from storage.artifacts import ArtifactStore
from storage.db import StorageManager
from storage.payloads import GENERAL_PAYLOAD_LIMIT_BYTES, json_dumps, json_loads


@dataclass
class ApprovalRecord:
    request_id: int
    method: str
    params: dict[str, Any]
    status: str = "pending"
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    session_id: str | None = None
    thread_id: str | None = None
    turn_id: str | None = None
    trace_id: str | None = None
    resolved_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "method": self.method,
            "params": self.params,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "session_id": self.session_id,
            "thread_id": self.thread_id,
            "turn_id": self.turn_id,
            "trace_id": self.trace_id,
            "resolved_at": self.resolved_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ApprovalRecord":
        return cls(
            request_id=int(data["request_id"]),
            method=data["method"],
            params=data.get("params", {}),
            status=data.get("status", "pending"),
            created_at=data.get("created_at", utc_now()),
            updated_at=data.get("updated_at", data.get("resolved_at", data.get("created_at", utc_now()))),
            session_id=data.get("session_id"),
            thread_id=data.get("thread_id"),
            turn_id=data.get("turn_id"),
            trace_id=data.get("trace_id"),
            resolved_at=data.get("resolved_at"),
        )


@dataclass
class ApprovalStoreState:
    approvals: list[ApprovalRecord] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"approvals": [approval.to_dict() for approval in self.approvals]}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ApprovalStoreState":
        return cls(approvals=[ApprovalRecord.from_dict(item) for item in data.get("approvals", [])])


class ApprovalStore:
    def __init__(self, paths: AppPaths):
        self.paths = paths
        self.storage = StorageManager(paths)
        self.artifacts = ArtifactStore(paths)

    def _deserialize_params(self, raw_value: str) -> dict[str, Any]:
        loaded = json_loads(raw_value, {})
        if ArtifactStore.is_reference(loaded):
            resolved = self.artifacts.read_json(loaded, {})
            return resolved if isinstance(resolved, dict) else {}
        return loaded if isinstance(loaded, dict) else {}

    def _serialize_params(self, params: dict[str, Any], *, connection=None) -> str:
        params_json = json_dumps(params)
        if len(params_json.encode("utf-8")) <= GENERAL_PAYLOAD_LIMIT_BYTES:
            return params_json
        artifact_ref = self.artifacts.write_text(
            kind="approval_params",
            text=params_json,
            suffix=".json",
            connection=connection,
        )
        return json_dumps(artifact_ref)

    @staticmethod
    def _row_to_record(row, params: dict[str, Any]) -> ApprovalRecord:
        return ApprovalRecord(
            request_id=int(row["request_id"]),
            method=str(row["method"]),
            params=params,
            status=str(row["status"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            session_id=row["session_id"],
            thread_id=row["thread_id"],
            turn_id=row["turn_id"],
            trace_id=row["trace_id"],
            resolved_at=row["resolved_at"],
        )

    def _upsert(self, connection, approval: ApprovalRecord) -> None:
        params_json = self._serialize_params(approval.params, connection=connection)
        connection.execute(
            """
            INSERT INTO approvals(
                request_id, session_id, thread_id, turn_id, trace_id, method,
                params_json, status, created_at, updated_at, resolved_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(request_id) DO UPDATE SET
                session_id = excluded.session_id,
                thread_id = excluded.thread_id,
                turn_id = excluded.turn_id,
                trace_id = excluded.trace_id,
                method = excluded.method,
                params_json = excluded.params_json,
                status = excluded.status,
                created_at = excluded.created_at,
                updated_at = excluded.updated_at,
                resolved_at = excluded.resolved_at
            """,
            (
                approval.request_id,
                approval.session_id,
                approval.thread_id,
                approval.turn_id,
                approval.trace_id,
                approval.method,
                params_json,
                approval.status,
                approval.created_at,
                approval.updated_at,
                approval.resolved_at,
            ),
        )

    def load(self) -> ApprovalStoreState:
        with self.storage.read_connection() as connection:
            rows = connection.execute("SELECT * FROM approvals ORDER BY created_at, request_id").fetchall()
        return ApprovalStoreState([self._row_to_record(row, self._deserialize_params(row["params_json"])) for row in rows])

    def save(self, state: ApprovalStoreState) -> None:
        with self.storage.transaction() as connection:
            connection.execute("DELETE FROM approvals")
            for approval in state.approvals:
                self._upsert(connection, approval)

    def add(self, approval: ApprovalRecord) -> None:
        with self.storage.transaction() as connection:
            self._upsert(connection, approval)

    def get_pending(self, request_id: int) -> ApprovalRecord | None:
        with self.storage.read_connection() as connection:
            row = connection.execute(
                "SELECT * FROM approvals WHERE request_id = ? AND status = 'pending'",
                (request_id,),
            ).fetchone()
        return self._row_to_record(row, self._deserialize_params(row["params_json"])) if row is not None else None

    def mark(self, request_id: int, status: str) -> ApprovalRecord | None:
        now = utc_now()
        with self.storage.transaction() as connection:
            row = connection.execute("SELECT * FROM approvals WHERE request_id = ?", (request_id,)).fetchone()
            if row is None:
                return None
            connection.execute(
                """
                UPDATE approvals
                SET status = ?, updated_at = ?, resolved_at = ?
                WHERE request_id = ?
                """,
                (status, now, None if status in {"pending", "stale"} else now, request_id),
            )
            updated = connection.execute("SELECT * FROM approvals WHERE request_id = ?", (request_id,)).fetchone()
        return self._row_to_record(updated, self._deserialize_params(updated["params_json"])) if updated is not None else None

    def pending(self) -> list[ApprovalRecord]:
        with self.storage.read_connection() as connection:
            rows = connection.execute("SELECT * FROM approvals WHERE status = 'pending' ORDER BY created_at, request_id").fetchall()
        return [self._row_to_record(row, self._deserialize_params(row["params_json"])) for row in rows]

    def stale(self) -> list[ApprovalRecord]:
        with self.storage.read_connection() as connection:
            rows = connection.execute("SELECT * FROM approvals WHERE status = 'stale' ORDER BY created_at, request_id").fetchall()
        return [self._row_to_record(row, self._deserialize_params(row["params_json"])) for row in rows]

    def mark_all_pending_stale(self) -> int:
        now = utc_now()
        with self.storage.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE approvals
                SET status = 'stale', updated_at = ?
                WHERE status = 'pending'
                """,
                (now,),
            )
            return int(cursor.rowcount or 0)
