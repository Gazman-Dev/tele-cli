from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional


def utc_now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


@dataclass
class LockMetadata:
    pid: int
    hostname: str
    username: str
    started_at: Optional[str]
    mode: str
    timestamp: str
    app_version: str
    child_codex_pid: Optional[int] = None
    command: list[str] = field(default_factory=list)
    cwd: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LockMetadata":
        return cls(**data)


@dataclass
class SetupState:
    status: str
    pid: int
    timestamp: str
    npm_installed: bool = False
    codex_installed: bool = False
    telegram_token_saved: bool = False
    telegram_validated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SetupState":
        return cls(**data)


@dataclass
class AuthState:
    bot_token: str
    telegram_user_id: Optional[int] = None
    telegram_chat_id: Optional[int] = None
    pairing_code: Optional[str] = None
    paired_at: Optional[str] = None
    pending_user_id: Optional[int] = None
    pending_chat_id: Optional[int] = None
    pending_issued_at: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AuthState":
        return cls(**data)


@dataclass
class Config:
    state_dir: str
    codex_command: list[str] = field(default_factory=lambda: ["codex"])
    sandbox_mode: str = "danger-full-access"
    approval_policy: str = "never"
    codex_personality: str = "pragmatic"
    codex_restart_backoff_seconds: float = 5.0
    codex_restart_backoff_max_seconds: float = 60.0
    telegram_backoff_seconds: float = 5.0
    telegram_backoff_max_seconds: float = 60.0
    partial_flush_idle_seconds: float = 3.0
    typing_indicator_interval_seconds: float = 4.0
    poll_interval_seconds: float = 2.0
    install_homebrew_if_missing: bool = False
    sleep_hour_local: int = 2

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Config":
        return cls(**data)


@dataclass
class RuntimeState:
    session_id: str
    service_state: str
    codex_state: str
    telegram_state: str
    recorder_state: str
    debug_state: str
    codex_pid: Optional[int] = None
    started_at: str = field(default_factory=utc_now)
    last_output_at: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RuntimeState":
        return cls(**data)


@dataclass
class CodexServerState:
    transport: str
    initialized: bool
    protocol_version: Optional[str] = None
    account_status: Optional[str] = None
    account_type: Optional[str] = None
    auth_required: bool = False
    login_type: Optional[str] = None
    login_url: Optional[str] = None
    pid: Optional[int] = None
    capabilities: dict[str, Any] = field(default_factory=dict)
    last_error: Optional[str] = None
    updated_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CodexServerState":
        return cls(**data)


@dataclass
class SessionRecord:
    session_id: str
    transport: str
    transport_user_id: Optional[int]
    transport_chat_id: Optional[int]
    transport_topic_id: Optional[int] = None
    transport_channel: Optional[str] = None
    attached: bool = True
    thread_id: Optional[str] = None
    active_turn_id: Optional[str] = None
    streaming_message_id: Optional[int] = None
    streaming_output_text: str = ""
    thinking_message_text: str = ""
    pending_output_text: str = ""
    pending_output_updated_at: Optional[str] = None
    last_completed_turn_id: Optional[str] = None
    last_delivered_output_text: str = ""
    status: str = "ACTIVE"
    instructions_dirty: bool = True
    last_seen_generation: int = 0
    created_at: str = field(default_factory=utc_now)
    last_user_message_at: Optional[str] = None
    last_agent_message_at: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SessionRecord":
        return cls(**data)
