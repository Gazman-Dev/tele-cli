from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.models import utc_now
from core.paths import AppPaths


def load_logging_health(paths: AppPaths) -> dict[str, Any]:
    try:
        raw = paths.logging_health.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"state": "healthy"}
    except OSError:
        return {"state": "degraded", "error": "logging_health_unreadable"}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {"state": "degraded", "error": "logging_health_invalid"}
    if not isinstance(data, dict):
        return {"state": "degraded", "error": "logging_health_invalid"}
    return data


def mark_logging_degraded(
    paths: AppPaths,
    *,
    operation: str,
    error: str,
    source: str | None = None,
    event_type: str | None = None,
) -> dict[str, Any]:
    existing = load_logging_health(paths)
    record = {
        "state": "degraded",
        "degraded_at": existing.get("degraded_at") or utc_now(),
        "last_error_at": utc_now(),
        "operation": operation,
        "error": error,
        "source": source,
        "event_type": event_type,
    }
    _write_health_record(paths.logging_health, record)
    append_emergency_log(
        paths,
        {
            "timestamp": utc_now(),
            "state": "degraded",
            "operation": operation,
            "error": error,
            "source": source,
            "event_type": event_type,
        },
    )
    return record


def clear_logging_degraded(paths: AppPaths) -> dict[str, Any] | None:
    existing = load_logging_health(paths)
    if existing.get("state") != "degraded":
        return None
    record = {
        "state": "healthy",
        "recovered_at": utc_now(),
    }
    _write_health_record(paths.logging_health, record)
    append_emergency_log(
        paths,
        {
            "timestamp": utc_now(),
            "state": "recovered",
            "prior_degraded_at": existing.get("degraded_at"),
            "prior_error": existing.get("error"),
            "prior_operation": existing.get("operation"),
            "prior_source": existing.get("source"),
            "prior_event_type": existing.get("event_type"),
        },
    )
    return existing


def append_emergency_log(paths: AppPaths, payload: dict[str, Any]) -> None:
    try:
        paths.logging_emergency_log.parent.mkdir(parents=True, exist_ok=True)
        with paths.logging_emergency_log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True) + "\n")
    except OSError:
        return


def _write_health_record(path: Path, payload: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    except OSError:
        return
