from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from core.json_store import load_json, save_json

STATE_SCHEMA_VERSION = 1


class StateMigrationError(RuntimeError):
    pass


@dataclass(frozen=True)
class VersionedStateEnvelope:
    version: int
    payload: dict

    def to_dict(self) -> dict:
        return {"version": self.version, "payload": self.payload}

    @classmethod
    def from_dict(cls, data: dict) -> "VersionedStateEnvelope":
        if "version" in data and "payload" in data:
            return cls(version=int(data["version"]), payload=dict(data["payload"]))
        return cls(version=0, payload=dict(data))


def load_versioned_state(path: Path, payload_factory: Callable[[dict], object]):
    envelope = load_json(path, VersionedStateEnvelope.from_dict)
    if envelope is None:
        return None
    if envelope.version > STATE_SCHEMA_VERSION:
        raise StateMigrationError(f"Unsupported future state version {envelope.version} in {path.name}.")
    migrated = migrate_state_payload(path, envelope)
    return payload_factory(migrated.payload)


def save_versioned_state(path: Path, payload: dict) -> None:
    save_json(path, VersionedStateEnvelope(version=STATE_SCHEMA_VERSION, payload=payload).to_dict())


def migrate_state_payload(path: Path, envelope: VersionedStateEnvelope) -> VersionedStateEnvelope:
    if envelope.version == STATE_SCHEMA_VERSION:
        return envelope
    if envelope.version == 0:
        migrated = VersionedStateEnvelope(version=STATE_SCHEMA_VERSION, payload=envelope.payload)
        save_json(path, migrated.to_dict())
        return migrated
    raise StateMigrationError(f"No migration path for state version {envelope.version} in {path.name}.")
