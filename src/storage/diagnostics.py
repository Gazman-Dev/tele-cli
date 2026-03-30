from __future__ import annotations

from core.paths import AppPaths

from .operations import TraceStore


def log_recovery_event(paths: AppPaths, message: str, *, event_type: str = "service.recovery") -> None:
    TraceStore(paths).log_event(
        source="service",
        event_type=event_type,
        payload={"message": message},
    )
