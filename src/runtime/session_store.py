from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from core.models import AuthState, SessionRecord, utc_now
from core.paths import AppPaths
from storage.db import StorageManager
from storage.operations import TraceStore
from storage.payloads import json_dumps, json_loads

from .instructions import session_short_memory_path, session_short_memory_relpath
from .workspaces import WorkspaceManager

_SESSION_STORE_LOCK = threading.RLock()


@dataclass
class SessionStoreState:
    sessions: list[SessionRecord] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"sessions": [session.to_dict() for session in self.sessions]}

    @classmethod
    def from_dict(cls, data: dict) -> "SessionStoreState":
        return cls(sessions=[SessionRecord.from_dict(item) for item in data.get("sessions", [])])


class SessionStore:
    def __init__(self, paths: AppPaths):
        self.paths = paths
        self.storage = StorageManager(paths)
        self.workspace_manager = WorkspaceManager(paths)
        self.trace_store = TraceStore(paths)

    def _log_session_event(self, event_type: str, session: SessionRecord, *, payload: dict | None = None) -> None:
        self.trace_store.log_event(
            source="session",
            event_type=event_type,
            session_id=session.session_id,
            thread_id=session.thread_id,
            turn_id=session.active_turn_id or session.last_completed_turn_id,
            chat_id=session.transport_chat_id,
            topic_id=session.transport_topic_id,
            payload=payload
            or {
                "transport": session.transport,
                "status": session.status,
                "attached": session.attached,
                "channel": session.transport_channel,
            },
        )

    def _log_detached_session_event(self, session: SessionRecord, *, reason: str) -> None:
        self.trace_store.log_event(
            source="session",
            event_type="session.detached",
            chat_id=session.transport_chat_id,
            topic_id=session.transport_topic_id,
            payload={
                "reason": reason,
                "detached_session_id": session.session_id,
                "transport": session.transport,
                "status": session.status,
                "channel": session.transport_channel,
            },
        )

    def _log_replaced_session_event(self, previous: SessionRecord, replacement: SessionRecord, *, reason: str) -> None:
        self.trace_store.log_event(
            source="session",
            event_type="session.replaced",
            session_id=replacement.session_id,
            thread_id=replacement.thread_id,
            turn_id=replacement.active_turn_id or replacement.last_completed_turn_id,
            chat_id=replacement.transport_chat_id,
            topic_id=replacement.transport_topic_id,
            payload={
                "reason": reason,
                "replaced_session_id": previous.session_id,
                "replacement_session_id": replacement.session_id,
                "transport": replacement.transport,
                "channel": replacement.transport_channel,
            },
        )

    @staticmethod
    def _stabilize_session(updated_session: SessionRecord, existing_session: SessionRecord | None = None) -> SessionRecord:
        stabilized = SessionRecord.from_dict(updated_session.to_dict())
        if stabilized.active_turn_id and not stabilized.thread_id and existing_session and existing_session.thread_id:
            stabilized.thread_id = existing_session.thread_id
        if stabilized.active_turn_id and not stabilized.thread_id:
            stabilized.active_turn_id = None
            if stabilized.status == "RUNNING_TURN":
                stabilized.status = "ACTIVE"
        return stabilized

    @staticmethod
    def _row_to_session(row) -> SessionRecord:
        payload = {
            "session_id": row["session_id"],
            "transport": row["transport"],
            "transport_user_id": row["transport_user_id"],
            "transport_chat_id": row["transport_chat_id"],
            "transport_topic_id": row["transport_topic_id"],
            "transport_channel": row["transport_channel"],
            "workspace_id": row["workspace_id"],
            "workspace_kind": row["workspace_kind"],
            "workspace_relpath": row["workspace_relpath"],
            "agents_md_relpath": row["agents_md_relpath"],
            "long_memory_relpath": row["long_memory_relpath"],
            "visible_topic_name": row["visible_topic_name"],
            "attached": bool(row["attached"]),
            "thread_id": row["thread_id"],
            "active_turn_id": row["active_turn_id"],
            "streaming_message_id": row["streaming_message_id"],
            "streaming_message_ids": json_loads(row["streaming_message_ids_json"], []),
            "thinking_message_id": row["thinking_message_id"],
            "thinking_message_ids": json_loads(row["thinking_message_ids_json"], []),
            "thinking_live_message_ids": json_loads(row["thinking_live_message_ids_json"], {}),
            "thinking_live_texts": json_loads(row["thinking_live_texts_json"], {}),
            "thinking_sent_texts": json_loads(row["thinking_sent_texts_json"], {}),
            "thinking_history_order": json_loads(row["thinking_history_order_json"], []),
            "thinking_history_by_source": json_loads(row["thinking_history_by_source_json"], {}),
            "streaming_output_text": row["streaming_output_text"],
            "streaming_phase": row["streaming_phase"],
            "thinking_message_text": row["thinking_message_text"],
            "thinking_history_text": row["thinking_history_text"],
            "last_thinking_sent_text": row["last_thinking_sent_text"],
            "pending_output_text": row["pending_output_text"],
            "queued_user_input_text": row["queued_user_input_text"],
            "pending_output_updated_at": row["pending_output_updated_at"],
            "last_completed_turn_id": row["last_completed_turn_id"],
            "last_delivered_output_text": row["last_delivered_output_text"],
            "status": row["status"],
            "instructions_dirty": bool(row["instructions_dirty"]),
            "last_seen_generation": int(row["last_seen_generation"]),
            "created_at": row["created_at"],
            "last_user_message_at": row["last_user_message_at"],
            "last_agent_message_at": row["last_agent_message_at"],
            "current_trace_id": row["current_trace_id"],
        }
        return SessionRecord.from_dict(payload)

    @staticmethod
    def _session_values(session: SessionRecord) -> tuple[object, ...]:
        return (
            session.session_id,
            session.transport,
            session.transport_user_id,
            session.transport_chat_id,
            session.transport_topic_id,
            session.transport_channel,
            session.workspace_id,
            session.workspace_kind,
            session.workspace_relpath,
            session.agents_md_relpath,
            session.long_memory_relpath,
            session.visible_topic_name,
            1 if session.attached else 0,
            session.status,
            session.thread_id,
            session.active_turn_id,
            session.last_completed_turn_id,
            getattr(session, "current_trace_id", None),
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
        )

    def _upsert_session(self, connection, session: SessionRecord) -> None:
        connection.execute(
            """
            INSERT INTO sessions(
                session_id, transport, transport_user_id, transport_chat_id, transport_topic_id, transport_channel,
                workspace_id, workspace_kind, workspace_relpath, agents_md_relpath, long_memory_relpath, visible_topic_name,
                attached, status, thread_id, active_turn_id, last_completed_turn_id, current_trace_id,
                instructions_dirty, last_seen_generation, created_at, last_user_message_at, last_agent_message_at,
                streaming_message_id, streaming_message_ids_json, thinking_message_id, thinking_message_ids_json,
                thinking_live_message_ids_json, thinking_live_texts_json, thinking_sent_texts_json,
                thinking_history_order_json, thinking_history_by_source_json, streaming_output_text, streaming_phase,
                thinking_message_text, thinking_history_text, last_thinking_sent_text, pending_output_text,
                queued_user_input_text, pending_output_updated_at, last_delivered_output_text
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            ON CONFLICT(session_id) DO UPDATE SET
                transport = excluded.transport,
                transport_user_id = excluded.transport_user_id,
                transport_chat_id = excluded.transport_chat_id,
                transport_topic_id = excluded.transport_topic_id,
                transport_channel = excluded.transport_channel,
                workspace_id = excluded.workspace_id,
                workspace_kind = excluded.workspace_kind,
                workspace_relpath = excluded.workspace_relpath,
                agents_md_relpath = excluded.agents_md_relpath,
                long_memory_relpath = excluded.long_memory_relpath,
                visible_topic_name = excluded.visible_topic_name,
                attached = excluded.attached,
                status = excluded.status,
                thread_id = excluded.thread_id,
                active_turn_id = excluded.active_turn_id,
                last_completed_turn_id = excluded.last_completed_turn_id,
                current_trace_id = excluded.current_trace_id,
                instructions_dirty = excluded.instructions_dirty,
                last_seen_generation = excluded.last_seen_generation,
                created_at = excluded.created_at,
                last_user_message_at = excluded.last_user_message_at,
                last_agent_message_at = excluded.last_agent_message_at,
                streaming_message_id = excluded.streaming_message_id,
                streaming_message_ids_json = excluded.streaming_message_ids_json,
                thinking_message_id = excluded.thinking_message_id,
                thinking_message_ids_json = excluded.thinking_message_ids_json,
                thinking_live_message_ids_json = excluded.thinking_live_message_ids_json,
                thinking_live_texts_json = excluded.thinking_live_texts_json,
                thinking_sent_texts_json = excluded.thinking_sent_texts_json,
                thinking_history_order_json = excluded.thinking_history_order_json,
                thinking_history_by_source_json = excluded.thinking_history_by_source_json,
                streaming_output_text = excluded.streaming_output_text,
                streaming_phase = excluded.streaming_phase,
                thinking_message_text = excluded.thinking_message_text,
                thinking_history_text = excluded.thinking_history_text,
                last_thinking_sent_text = excluded.last_thinking_sent_text,
                pending_output_text = excluded.pending_output_text,
                queued_user_input_text = excluded.queued_user_input_text,
                pending_output_updated_at = excluded.pending_output_updated_at,
                last_delivered_output_text = excluded.last_delivered_output_text
            """,
            self._session_values(session),
        )
        connection.execute(
            """
            INSERT INTO session_short_memory(session_id, short_memory_relpath, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                short_memory_relpath = excluded.short_memory_relpath,
                updated_at = excluded.updated_at
            """,
            (session.session_id, session_short_memory_relpath(session.session_id), utc_now()),
        )

    def _touch_short_memory(self, session_id: str, connection=None) -> None:
        path = self.short_memory_path(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(exist_ok=True)
        if connection is not None:
            connection.execute(
                """
                INSERT INTO session_short_memory(session_id, short_memory_relpath, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    short_memory_relpath = excluded.short_memory_relpath,
                    updated_at = excluded.updated_at
                """,
                (session_id, session_short_memory_relpath(session_id), utc_now()),
            )

    def _select_sessions(self, query: str = "", params: tuple[object, ...] = ()) -> list[SessionRecord]:
        sql = "SELECT * FROM sessions"
        if query:
            sql = f"{sql} {query}"
        with self.storage.read_connection() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [self._row_to_session(row) for row in rows]

    def _select_single_session(self, where: str, params: tuple[object, ...]) -> SessionRecord | None:
        rows = self._select_sessions(f"{where} LIMIT 1", params)
        return rows[0] if rows else None

    def load(self) -> SessionStoreState:
        with _SESSION_STORE_LOCK:
            return SessionStoreState(self._select_sessions("ORDER BY created_at, session_id"))

    def save(self, state: SessionStoreState) -> None:
        with _SESSION_STORE_LOCK:
            bound_sessions = [self.workspace_manager.bind_session(session) for session in state.sessions]
            session_ids = {session.session_id for session in state.sessions}
            with self.storage.transaction() as connection:
                protected_clause = """
                    AND NOT EXISTS (SELECT 1 FROM approvals WHERE approvals.session_id = sessions.session_id)
                    AND NOT EXISTS (SELECT 1 FROM traces WHERE traces.session_id = sessions.session_id)
                    AND NOT EXISTS (SELECT 1 FROM events WHERE events.session_id = sessions.session_id)
                    AND NOT EXISTS (
                        SELECT 1 FROM telegram_message_groups
                        WHERE telegram_message_groups.session_id = sessions.session_id
                    )
                """
                if session_ids:
                    placeholders = ",".join("?" for _ in session_ids)
                    connection.execute(
                        f"DELETE FROM sessions WHERE session_id NOT IN ({placeholders}) {protected_clause}",
                        tuple(session_ids),
                    )
                else:
                    connection.execute(f"DELETE FROM sessions WHERE 1 = 1 {protected_clause}")
                for bound in bound_sessions:
                    self._upsert_session(connection, bound)
                    self._touch_short_memory(bound.session_id, connection)

    @staticmethod
    def is_writable(session: SessionRecord) -> bool:
        return session.attached and session.status in {"ACTIVE", "RUNNING_TURN", "INTERRUPTED", "DELIVERING_FINAL"}

    @staticmethod
    def is_recoverable(session: SessionRecord) -> bool:
        return session.status in {"ACTIVE", "RUNNING_TURN", "INTERRUPTED", "RECOVERING_TURN", "DELIVERING_FINAL"}

    @staticmethod
    def is_prunable_detached(session: SessionRecord) -> bool:
        return not session.attached and session.active_turn_id is None and not session.pending_output_text

    @staticmethod
    def _matches_transport(session: SessionRecord, auth: AuthState, topic_id: int | None = None) -> bool:
        return (
            session.transport == "telegram"
            and session.transport_chat_id == auth.telegram_chat_id
            and session.transport_topic_id == topic_id
        )

    @staticmethod
    def _matches_local_channel(session: SessionRecord, channel: str) -> bool:
        return session.transport == "local" and session.transport_channel == channel

    def get_or_create_telegram_session(
        self,
        auth: AuthState,
        topic_id: int | None = None,
        *,
        visible_topic_name: str | None = None,
    ) -> SessionRecord:
        normalized_visible_name = (visible_topic_name or "").strip() or None
        with _SESSION_STORE_LOCK:
            state = self.load()
            matching = [s for s in state.sessions if self._matches_transport(s, auth, topic_id) and s.attached]
            active = next((s for s in matching if self.is_writable(s)), None)
            if active is not None:
                if normalized_visible_name and active.visible_topic_name != normalized_visible_name:
                    active.visible_topic_name = normalized_visible_name
                    self.save_session(active)
                self._log_session_event("session.reused", active)
                return active
            if matching:
                current = matching[-1]
                if normalized_visible_name and current.visible_topic_name != normalized_visible_name:
                    current.visible_topic_name = normalized_visible_name
                    self.save_session(current)
                self._log_session_event("session.reused", current, payload={"reason": "attached_non_writable", "status": current.status})
                return current
            session = SessionRecord(
                session_id=str(uuid.uuid4()),
                transport="telegram",
                transport_user_id=auth.telegram_user_id,
                transport_chat_id=auth.telegram_chat_id,
                transport_topic_id=topic_id,
                visible_topic_name=normalized_visible_name,
            )
            self.save_session(session)
            self.workspace_manager.ensure_session_workspace(session, visible_topic_name=normalized_visible_name)
            self._touch_short_memory(session.session_id)
            self._log_session_event("session.created", session)
            return session

    def save_session(self, updated_session: SessionRecord) -> None:
        with _SESSION_STORE_LOCK:
            existing = self._select_single_session("WHERE session_id = ?", (updated_session.session_id,))
            session = self._stabilize_session(updated_session, existing)
            session = self.workspace_manager.bind_session(session)
            with self.storage.transaction() as connection:
                self._upsert_session(connection, session)
                self._touch_short_memory(session.session_id, connection)

    def mark_user_message(self, session: SessionRecord) -> SessionRecord:
        session.last_user_message_at = utc_now()
        self.save_session(session)
        return session

    def mark_agent_message(self, session: SessionRecord) -> SessionRecord:
        session.last_agent_message_at = utc_now()
        self.save_session(session)
        return session

    def create_new_telegram_session(
        self,
        auth: AuthState,
        topic_id: int | None = None,
        *,
        visible_topic_name: str | None = None,
    ) -> SessionRecord:
        normalized_visible_name = (visible_topic_name or "").strip() or None
        with _SESSION_STORE_LOCK:
            state = self.load()
            replaced_sessions: list[SessionRecord] = []
            for session in state.sessions:
                if self._matches_transport(session, auth, topic_id):
                    session.attached = False
                    self.save_session(session)
                    self._log_detached_session_event(session, reason="new_session_requested")
                    replaced_sessions.append(SessionRecord.from_dict(session.to_dict()))
            refreshed = self.load()
            self._delete_prunable_session_ids([s.session_id for s in refreshed.sessions if self.is_prunable_detached(s)])
            session = SessionRecord(
                session_id=str(uuid.uuid4()),
                transport="telegram",
                transport_user_id=auth.telegram_user_id,
                transport_chat_id=auth.telegram_chat_id,
                transport_topic_id=topic_id,
                visible_topic_name=normalized_visible_name,
            )
            self.save_session(session)
            self.workspace_manager.ensure_session_workspace(session, visible_topic_name=normalized_visible_name)
            self._touch_short_memory(session.session_id)
            self._log_session_event("session.created", session, payload={"reason": "explicit_new"})
            for previous in replaced_sessions:
                self._log_replaced_session_event(previous, session, reason="explicit_new")
            return session

    def get_or_create_local_session(self, channel: str) -> SessionRecord:
        normalized = channel.strip()
        if not normalized:
            raise ValueError("Local channel name is required.")
        with _SESSION_STORE_LOCK:
            state = self.load()
            matching = [s for s in state.sessions if self._matches_local_channel(s, normalized) and s.attached]
            active = next((s for s in matching if self.is_writable(s)), None)
            if active is not None:
                self._log_session_event("session.reused", active)
                return active
            if matching:
                current = matching[-1]
                self._log_session_event("session.reused", current, payload={"reason": "attached_non_writable", "status": current.status})
                return current
            session = SessionRecord(
                session_id=str(uuid.uuid4()),
                transport="local",
                transport_user_id=None,
                transport_chat_id=None,
                transport_channel=normalized,
            )
            self.save_session(session)
            self.workspace_manager.ensure_session_workspace(session)
            self._touch_short_memory(session.session_id)
            self._log_session_event("session.created", session)
            return session

    def create_new_local_session(self, channel: str) -> SessionRecord:
        normalized = channel.strip()
        if not normalized:
            raise ValueError("Local channel name is required.")
        with _SESSION_STORE_LOCK:
            state = self.load()
            replaced_sessions: list[SessionRecord] = []
            for session in state.sessions:
                if self._matches_local_channel(session, normalized):
                    session.attached = False
                    self.save_session(session)
                    self._log_detached_session_event(session, reason="new_session_requested")
                    replaced_sessions.append(SessionRecord.from_dict(session.to_dict()))
            refreshed = self.load()
            self._delete_prunable_session_ids([s.session_id for s in refreshed.sessions if self.is_prunable_detached(s)])
            session = SessionRecord(
                session_id=str(uuid.uuid4()),
                transport="local",
                transport_user_id=None,
                transport_chat_id=None,
                transport_channel=normalized,
            )
            self.save_session(session)
            self.workspace_manager.ensure_session_workspace(session)
            self._touch_short_memory(session.session_id)
            self._log_session_event("session.created", session, payload={"reason": "explicit_new"})
            for previous in replaced_sessions:
                self._log_replaced_session_event(previous, session, reason="explicit_new")
            return session

    def list_local_sessions(self, channel: str) -> list[SessionRecord]:
        normalized = channel.strip()
        if not normalized:
            return []
        return self._select_sessions(
            "WHERE transport = 'local' AND transport_channel = ? ORDER BY created_at, session_id",
            (normalized,),
        )

    def get_current_local_session(self, channel: str) -> SessionRecord | None:
        sessions = list(reversed(self.list_local_sessions(channel)))
        for session in sessions:
            if session.attached and self.is_recoverable(session):
                return session
        return None

    def get_active_local_session(self, channel: str) -> SessionRecord | None:
        sessions = list(reversed(self.list_local_sessions(channel)))
        for session in sessions:
            if self.is_writable(session):
                return session
        return None

    def short_memory_path(self, session_id: str) -> Path:
        return session_short_memory_path(self.paths, session_id)

    def list_telegram_sessions(self, auth: AuthState, topic_id: int | None = None) -> list[SessionRecord]:
        return self._select_sessions(
            "WHERE transport = 'telegram' AND transport_chat_id IS ? AND transport_topic_id IS ? ORDER BY created_at, session_id",
            (auth.telegram_chat_id, topic_id),
        )

    def get_active_telegram_session(self, auth: AuthState, topic_id: int | None = None) -> SessionRecord | None:
        sessions = list(reversed(self.list_telegram_sessions(auth, topic_id)))
        for session in sessions:
            if self.is_writable(session):
                return session
        return None

    def get_current_telegram_session(self, auth: AuthState, topic_id: int | None = None) -> SessionRecord | None:
        sessions = list(reversed(self.list_telegram_sessions(auth, topic_id)))
        for session in sessions:
            if session.attached and self.is_recoverable(session):
                return session
        return None

    def has_recovering_session(self, auth: AuthState, topic_id: int | None = None) -> bool:
        sessions = self.list_telegram_sessions(auth, topic_id)
        return any(session.attached and session.status == "RECOVERING_TURN" for session in sessions)

    def find_by_turn_id(self, turn_id: str) -> SessionRecord | None:
        return self._select_single_session("WHERE active_turn_id = ?", (turn_id,))

    def find_by_thread_id(self, thread_id: str) -> SessionRecord | None:
        return self._select_single_session("WHERE thread_id = ?", (thread_id,))

    def find_by_completed_turn_id(self, turn_id: str) -> SessionRecord | None:
        return self._select_single_session("WHERE last_completed_turn_id = ?", (turn_id,))

    def append_pending_output(self, session: SessionRecord, text: str) -> SessionRecord:
        if text:
            session.pending_output_text += text
            session.pending_output_updated_at = utc_now()
            self.save_session(session)
        return session

    def consume_pending_output(self, session: SessionRecord) -> str:
        text = session.pending_output_text
        session.pending_output_text = ""
        session.pending_output_updated_at = None
        self.save_session(session)
        return text

    def mark_delivered_output(self, session: SessionRecord, text: str) -> SessionRecord:
        session.last_delivered_output_text = text
        self.save_session(session)
        return session

    def prune_detached_sessions(self) -> int:
        state = self.load()
        before = len(state.sessions)
        state.sessions = [s for s in state.sessions if not self.is_prunable_detached(s)]
        expected_removed = before - len(state.sessions)
        if expected_removed:
            self.save(state)
        after = len(self.load().sessions)
        return max(before - after, 0)

    def _delete_prunable_session_ids(self, session_ids: list[str]) -> None:
        if not session_ids:
            return
        placeholders = ",".join("?" for _ in session_ids)
        params = tuple(session_ids)
        with self.storage.transaction() as connection:
            connection.execute(
                f"""
                UPDATE events
                SET session_id = NULL
                WHERE session_id IN ({placeholders})
                  AND source = 'session'
                """,
                params,
            )
            connection.execute(
                f"""
                DELETE FROM sessions
                WHERE session_id IN ({placeholders})
                  AND NOT EXISTS (SELECT 1 FROM approvals WHERE approvals.session_id = sessions.session_id)
                  AND NOT EXISTS (SELECT 1 FROM traces WHERE traces.session_id = sessions.session_id)
                  AND NOT EXISTS (SELECT 1 FROM events WHERE events.session_id = sessions.session_id)
                  AND NOT EXISTS (
                        SELECT 1 FROM telegram_message_groups
                        WHERE telegram_message_groups.session_id = sessions.session_id
                    )
                """,
                params,
            )

    def mark_recovering_turns(self) -> list[SessionRecord]:
        state = self.load()
        recovering: list[SessionRecord] = []
        changed = False
        for session in state.sessions:
            if session.status == "RUNNING_TURN" and session.active_turn_id:
                session.status = "RECOVERING_TURN"
                recovering.append(session)
                changed = True
        if changed:
            self.save(state)
            for session in recovering:
                self._log_session_event("session.recovered", session, payload={"reason": "startup_recovery"})
        return recovering
