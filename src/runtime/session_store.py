from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path

from core.models import AuthState, SessionRecord, utc_now
from core.paths import AppPaths
from core.state_versions import load_versioned_state, save_versioned_state

from .instructions import session_short_memory_path


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

    def load(self) -> SessionStoreState:
        return load_versioned_state(self.paths.sessions, SessionStoreState.from_dict) or SessionStoreState()

    def save(self, state: SessionStoreState) -> None:
        save_versioned_state(self.paths.sessions, state.to_dict())

    @staticmethod
    def is_writable(session: SessionRecord) -> bool:
        return session.attached and session.status in {"ACTIVE", "RUNNING_TURN", "INTERRUPTED"}

    @staticmethod
    def is_recoverable(session: SessionRecord) -> bool:
        return session.status in {"ACTIVE", "RUNNING_TURN", "INTERRUPTED", "RECOVERING_TURN"}

    @staticmethod
    def is_prunable_detached(session: SessionRecord) -> bool:
        return (
            not session.attached
            and session.active_turn_id is None
            and not session.pending_output_text
        )

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

    def _touch_short_memory(self, session_id: str) -> None:
        path = self.short_memory_path(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(exist_ok=True)

    def get_or_create_telegram_session(self, auth: AuthState, topic_id: int | None = None) -> SessionRecord:
        state = self.load()
        matching = [
            session
            for session in state.sessions
            if self._matches_transport(session, auth, topic_id) and session.attached
        ]
        active = next((session for session in matching if self.is_writable(session)), None)
        if active is not None:
            return active
        if matching:
            return matching[-1]
        session = SessionRecord(
            session_id=str(uuid.uuid4()),
            transport="telegram",
            transport_user_id=auth.telegram_user_id,
            transport_chat_id=auth.telegram_chat_id,
            transport_topic_id=topic_id,
        )
        state.sessions.append(session)
        self.save(state)
        self._touch_short_memory(session.session_id)
        return session

    def save_session(self, updated_session: SessionRecord) -> None:
        state = self.load()
        for index, session in enumerate(state.sessions):
            if session.session_id == updated_session.session_id:
                state.sessions[index] = updated_session
                self.save(state)
                return
        state.sessions.append(updated_session)
        self.save(state)

    def mark_user_message(self, session: SessionRecord) -> SessionRecord:
        session.last_user_message_at = utc_now()
        self.save_session(session)
        return session

    def mark_agent_message(self, session: SessionRecord) -> SessionRecord:
        session.last_agent_message_at = utc_now()
        self.save_session(session)
        return session

    def create_new_telegram_session(self, auth: AuthState, topic_id: int | None = None) -> SessionRecord:
        state = self.load()
        for session in state.sessions:
            if self._matches_transport(session, auth, topic_id):
                session.attached = False
        state.sessions = [session for session in state.sessions if not self.is_prunable_detached(session)]
        session = SessionRecord(
            session_id=str(uuid.uuid4()),
            transport="telegram",
            transport_user_id=auth.telegram_user_id,
            transport_chat_id=auth.telegram_chat_id,
            transport_topic_id=topic_id,
        )
        state.sessions.append(session)
        self.save(state)
        self._touch_short_memory(session.session_id)
        return session

    def get_or_create_local_session(self, channel: str) -> SessionRecord:
        normalized = channel.strip()
        if not normalized:
            raise ValueError("Local channel name is required.")
        state = self.load()
        matching = [
            session
            for session in state.sessions
            if self._matches_local_channel(session, normalized) and session.attached
        ]
        active = next((session for session in matching if self.is_writable(session)), None)
        if active is not None:
            return active
        if matching:
            return matching[-1]
        session = SessionRecord(
            session_id=str(uuid.uuid4()),
            transport="local",
            transport_user_id=None,
            transport_chat_id=None,
            transport_channel=normalized,
        )
        state.sessions.append(session)
        self.save(state)
        self._touch_short_memory(session.session_id)
        return session

    def create_new_local_session(self, channel: str) -> SessionRecord:
        normalized = channel.strip()
        if not normalized:
            raise ValueError("Local channel name is required.")
        state = self.load()
        for session in state.sessions:
            if self._matches_local_channel(session, normalized):
                session.attached = False
        state.sessions = [session for session in state.sessions if not self.is_prunable_detached(session)]
        session = SessionRecord(
            session_id=str(uuid.uuid4()),
            transport="local",
            transport_user_id=None,
            transport_chat_id=None,
            transport_channel=normalized,
        )
        state.sessions.append(session)
        self.save(state)
        self._touch_short_memory(session.session_id)
        return session

    def list_local_sessions(self, channel: str) -> list[SessionRecord]:
        normalized = channel.strip()
        if not normalized:
            return []
        state = self.load()
        return [
            session
            for session in state.sessions
            if self._matches_local_channel(session, normalized)
        ]

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
        state = self.load()
        return [
            session
            for session in state.sessions
            if self._matches_transport(session, auth, topic_id)
        ]

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
        state = self.load()
        for session in state.sessions:
            if session.active_turn_id == turn_id:
                return session
        return None

    def find_by_thread_id(self, thread_id: str) -> SessionRecord | None:
        state = self.load()
        for session in state.sessions:
            if session.thread_id == thread_id:
                return session
        return None

    def find_by_completed_turn_id(self, turn_id: str) -> SessionRecord | None:
        state = self.load()
        for session in state.sessions:
            if session.last_completed_turn_id == turn_id:
                return session
        return None

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
        state.sessions = [session for session in state.sessions if not self.is_prunable_detached(session)]
        removed = before - len(state.sessions)
        if removed:
            self.save(state)
        return removed

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
        return recovering
