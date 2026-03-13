from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any


def utc_now() -> str:
    return datetime.now(tz=UTC).isoformat()


@dataclass
class LockMetadata:
    pid: int
    hostname: str
    username: str
    started_at: str | None
    mode: str
    timestamp: str
    app_version: str
    child_codex_pid: int | None = None
    command: list[str] = field(default_factory=list)
    cwd: str | None = None

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
    telegram_user_id: int | None = None
    telegram_chat_id: int | None = None
    pairing_code: str | None = None
    paired_at: str | None = None
    pending_user_id: int | None = None
    pending_chat_id: int | None = None
    pending_issued_at: str | None = None

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
    codex_pid: int | None = None
    started_at: str = field(default_factory=utc_now)
    last_output_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RuntimeState":
        return cls(**data)
