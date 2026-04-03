from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core.paths import AppPaths

from .db import StorageManager
from .operations import TraceStore


@dataclass(frozen=True)
class LogRetentionPolicy:
    event_days: int = 30
    trace_days: int = 30
    queue_days: int = 14
    service_run_days: int = 90
    mirror_max_bytes: int = 1_000_000
    mirror_backups: int = 5


def rotate_mirror_log(path: Path, *, max_bytes: int, backups: int) -> bool:
    if max_bytes <= 0 or backups <= 0 or not path.exists():
        return False
    try:
        if path.stat().st_size <= max_bytes:
            return False
    except OSError:
        return False
    oldest = path.with_name(f"{path.name}.{backups}")
    if oldest.exists():
        oldest.unlink()
    for index in range(backups - 1, 0, -1):
        source = path.with_name(f"{path.name}.{index}")
        target = path.with_name(f"{path.name}.{index + 1}")
        if source.exists():
            source.replace(target)
    path.replace(path.with_name(f"{path.name}.1"))
    return True


def prune_logs(
    paths: AppPaths,
    *,
    policy: LogRetentionPolicy | None = None,
    now: datetime | None = None,
    run_id: str | None = None,
) -> dict[str, int]:
    effective_policy = policy or LogRetentionPolicy()
    current = now or datetime.now(timezone.utc)
    event_cutoff = (current - timedelta(days=effective_policy.event_days)).isoformat()
    trace_cutoff = (current - timedelta(days=effective_policy.trace_days)).isoformat()
    queue_cutoff = (current - timedelta(days=effective_policy.queue_days)).isoformat()
    run_cutoff = (current - timedelta(days=effective_policy.service_run_days)).isoformat()
    rotated_terminal = 1 if rotate_mirror_log(paths.terminal_log, max_bytes=effective_policy.mirror_max_bytes, backups=effective_policy.mirror_backups) else 0
    rotated_performance = 1 if rotate_mirror_log(paths.performance_log, max_bytes=effective_policy.mirror_max_bytes, backups=effective_policy.mirror_backups) else 0

    storage = StorageManager(paths)
    artifact_relpaths_to_delete: list[str] = []
    with storage.transaction() as connection:
        event_artifacts = connection.execute(
            """
            SELECT DISTINCT a.relpath
            FROM artifacts a
            JOIN events e ON e.artifact_id = a.artifact_id
            LEFT JOIN traces t ON t.trace_id = e.trace_id
            WHERE e.received_at < ?
              AND (e.trace_id IS NULL OR (t.completed_at IS NOT NULL AND t.completed_at < ?))
            """,
            (event_cutoff, trace_cutoff),
        ).fetchall()
        artifact_relpaths_to_delete.extend(str(row["relpath"]) for row in event_artifacts)
        deleted_events = connection.execute(
            """
            DELETE FROM events
            WHERE received_at < ?
              AND (
                    trace_id IS NULL
                    OR trace_id IN (
                        SELECT trace_id FROM traces
                        WHERE completed_at IS NOT NULL AND completed_at < ?
                    )
                )
            """,
            (event_cutoff, trace_cutoff),
        ).rowcount
        deleted_queue = connection.execute(
            """
            DELETE FROM telegram_outbound_queue
            WHERE status IN ('completed', 'failed')
              AND COALESCE(completed_at, created_at) < ?
            """,
            (queue_cutoff,),
        ).rowcount
        deleted_traces = connection.execute(
            """
            DELETE FROM traces
            WHERE completed_at IS NOT NULL
              AND completed_at < ?
              AND NOT EXISTS (SELECT 1 FROM events WHERE events.trace_id = traces.trace_id)
              AND NOT EXISTS (SELECT 1 FROM approvals WHERE approvals.trace_id = traces.trace_id)
              AND NOT EXISTS (SELECT 1 FROM telegram_outbound_queue WHERE telegram_outbound_queue.trace_id = traces.trace_id)
              AND NOT EXISTS (SELECT 1 FROM telegram_message_groups WHERE telegram_message_groups.trace_id = traces.trace_id)
            """,
            (trace_cutoff,),
        ).rowcount
        deleted_runs = connection.execute(
            """
            DELETE FROM service_runs
            WHERE stopped_at IS NOT NULL
              AND stopped_at < ?
              AND NOT EXISTS (SELECT 1 FROM events WHERE events.run_id = service_runs.run_id)
              AND NOT EXISTS (
                    SELECT 1 FROM telegram_outbound_queue
                    WHERE telegram_outbound_queue.claimed_by_run_id = service_runs.run_id
                )
            """,
            (run_cutoff,),
        ).rowcount
        deleted_event_artifacts = connection.execute(
            """
            DELETE FROM artifacts
            WHERE kind LIKE 'event_%'
              AND NOT EXISTS (SELECT 1 FROM events WHERE events.artifact_id = artifacts.artifact_id)
            """,
        ).rowcount
    for relpath in artifact_relpaths_to_delete:
        try:
            (paths.root / relpath).unlink(missing_ok=True)
        except OSError:
            pass
    summary = {
        "deleted_events": int(deleted_events or 0),
        "deleted_queue_rows": int(deleted_queue or 0),
        "deleted_traces": int(deleted_traces or 0),
        "deleted_service_runs": int(deleted_runs or 0),
        "deleted_event_artifacts": int(deleted_event_artifacts or 0),
        "rotated_terminal_log": rotated_terminal,
        "rotated_performance_log": rotated_performance,
    }
    if any(summary.values()):
        TraceStore(paths, run_id=run_id).log_event(
            source="storage",
            event_type="logging.pruned",
            payload=summary,
        )
    return summary
