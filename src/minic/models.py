from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Optional


def utc_now() -> str:
    return datetime.now(tz=UTC).isoformat()


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
    poll_interval_seconds: float = 2.0
    install_homebrew_if_missing: bool = False

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
