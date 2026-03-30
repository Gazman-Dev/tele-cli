from __future__ import annotations

import sqlite3
from typing import Callable, TypeVar

from core.models import CodexServerState, RuntimeState, utc_now
from core.paths import AppPaths

from .db import StorageManager
from .payloads import json_dumps, json_loads


T = TypeVar("T")


def _load_state(paths: AppPaths, key: str, factory: Callable[[dict], T]) -> T | None:
    storage = StorageManager(paths)
    with storage.read_connection() as connection:
        row = connection.execute(
            "SELECT value_json FROM app_state WHERE state_key = ?",
            (key,),
        ).fetchone()
    if row is None:
        return None
    return factory(json_loads(row["value_json"], {}))


def _save_state(paths: AppPaths, key: str, value: dict) -> None:
    storage = StorageManager(paths)
    with storage.transaction() as connection:
        connection.execute(
            """
            INSERT INTO app_state(state_key, value_json, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(state_key) DO UPDATE SET
                value_json = excluded.value_json,
                updated_at = excluded.updated_at
            """,
            (key, json_dumps(value), utc_now()),
        )


def load_runtime_state(paths: AppPaths) -> RuntimeState | None:
    return _load_state(paths, "runtime", RuntimeState.from_dict)


def save_runtime_state(paths: AppPaths, state: RuntimeState) -> None:
    _save_state(paths, "runtime", state.to_dict())


def load_codex_server_state(paths: AppPaths) -> CodexServerState | None:
    return _load_state(paths, "codex_server", CodexServerState.from_dict)


def save_codex_server_state(paths: AppPaths, state: CodexServerState) -> None:
    _save_state(paths, "codex_server", state.to_dict())
