from __future__ import annotations

import json
import sqlite3
from typing import Any

from core.paths import AppPaths


def _read_rows(paths: AppPaths, query: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    connection = sqlite3.connect(paths.database)
    connection.row_factory = sqlite3.Row
    try:
        return connection.execute(query, params).fetchall()
    finally:
        connection.close()


def _format_payload(row: sqlite3.Row) -> str:
    payload_json = row["payload_json"] if "payload_json" in row.keys() else None
    payload_preview = row["payload_preview"] if "payload_preview" in row.keys() else None
    if isinstance(payload_json, str) and payload_json:
        try:
            payload = json.loads(payload_json)
            if isinstance(payload, dict):
                if "message" in payload and isinstance(payload["message"], str):
                    return payload["message"]
                if "error" in payload and isinstance(payload["error"], str):
                    return payload["error"]
            return payload_json
        except Exception:
            return payload_json
    if isinstance(payload_preview, str):
        return payload_preview
    return ""


def _print_event_rows(rows: list[sqlite3.Row]) -> None:
    if not rows:
        print("No matching events.")
        return
    for row in rows:
        timestamp = row["received_at"]
        source = row["source"]
        event_type = row["event_type"]
        trace_id = row["trace_id"] or "-"
        session_id = row["session_id"] or "-"
        detail = _format_payload(row)
        print(f"{timestamp} {source} {event_type} trace={trace_id} session={session_id}")
        if detail:
            print(f"  {detail}")


def _run_recent(paths: AppPaths, *, limit: int, source: str | None, event_type: str | None) -> None:
    where: list[str] = []
    params: list[Any] = []
    if source:
        where.append("source = ?")
        params.append(source)
    if event_type:
        where.append("event_type = ?")
        params.append(event_type)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    rows = _read_rows(
        paths,
        f"""
        SELECT received_at, source, event_type, trace_id, session_id, payload_json, payload_preview
        FROM events
        {clause}
        ORDER BY event_id DESC
        LIMIT ?
        """,
        (*params, int(limit)),
    )
    _print_event_rows(list(reversed(rows)))


def _run_failures(paths: AppPaths, *, limit: int) -> None:
    rows = _read_rows(
        paths,
        """
        SELECT received_at, source, event_type, trace_id, session_id, payload_json, payload_preview
        FROM events
        WHERE event_type LIKE '%.failed'
           OR event_type LIKE '%error%'
           OR (source = 'service' AND event_type = 'service.recovery')
        ORDER BY event_id DESC
        LIMIT ?
        """,
        (int(limit),),
    )
    _print_event_rows(list(reversed(rows)))


def _run_trace(paths: AppPaths, *, trace_id: str) -> None:
    trace_rows = _read_rows(
        paths,
        """
        SELECT trace_id, session_id, thread_id, turn_id, chat_id, topic_id, user_text_preview, started_at, completed_at, outcome
        FROM traces
        WHERE trace_id = ?
        """,
        (trace_id,),
    )
    if not trace_rows:
        raise SystemExit(f"Trace not found: {trace_id}")
    trace = trace_rows[0]
    print(
        f"trace={trace['trace_id']} session={trace['session_id'] or '-'} "
        f"thread={trace['thread_id'] or '-'} turn={trace['turn_id'] or '-'} "
        f"chat={trace['chat_id'] if trace['chat_id'] is not None else '-'} "
        f"topic={trace['topic_id'] if trace['topic_id'] is not None else '-'} "
        f"outcome={trace['outcome'] or '-'}"
    )
    if trace["user_text_preview"]:
        print(f"user={trace['user_text_preview']}")
    rows = _read_rows(
        paths,
        """
        SELECT received_at, source, event_type, trace_id, session_id, payload_json, payload_preview
        FROM events
        WHERE trace_id = ?
        ORDER BY event_id ASC
        """,
        (trace_id,),
    )
    _print_event_rows(rows)


def _run_queue(paths: AppPaths, *, limit: int, status: str | None) -> None:
    where = "WHERE status = ?" if status else ""
    params: tuple[Any, ...] = (status, int(limit)) if status else (int(limit),)
    rows = _read_rows(
        paths,
        f"""
        SELECT queue_id, created_at, available_at, status, op_type, chat_id, topic_id, session_id, trace_id,
               telegram_message_id, dedupe_key, priority, attempt_count, last_error, completed_at
        FROM telegram_outbound_queue
        {where}
        ORDER BY created_at DESC
        LIMIT ?
        """,
        params,
    )
    if not rows:
        print("No matching queue rows.")
        return
    for row in reversed(rows):
        print(
            f"{row['created_at']} {row['status']} {row['op_type']} queue={row['queue_id']} "
            f"chat={row['chat_id']} topic={row['topic_id'] if row['topic_id'] is not None else '-'} "
            f"trace={row['trace_id'] or '-'} attempts={row['attempt_count']}"
        )
        if row["last_error"]:
            print(f"  {row['last_error']}")


def _run_session(paths: AppPaths, *, session_id: str, limit: int) -> None:
    rows = _read_rows(
        paths,
        """
        SELECT received_at, source, event_type, trace_id, session_id, payload_json, payload_preview
        FROM events
        WHERE session_id = ?
        ORDER BY event_id DESC
        LIMIT ?
        """,
        (session_id, int(limit)),
    )
    _print_event_rows(list(reversed(rows)))


def _run_chat(paths: AppPaths, *, chat_id: int, topic_id: int | None, limit: int) -> None:
    if topic_id is None:
        rows = _read_rows(
            paths,
            """
            SELECT received_at, source, event_type, trace_id, session_id, payload_json, payload_preview
            FROM events
            WHERE chat_id = ?
            ORDER BY event_id DESC
            LIMIT ?
            """,
            (int(chat_id), int(limit)),
        )
    else:
        rows = _read_rows(
            paths,
            """
            SELECT received_at, source, event_type, trace_id, session_id, payload_json, payload_preview
            FROM events
            WHERE chat_id = ? AND topic_id IS ?
            ORDER BY event_id DESC
            LIMIT ?
            """,
            (int(chat_id), topic_id, int(limit)),
        )
    _print_event_rows(list(reversed(rows)))


def run_logs_command(paths: AppPaths, args) -> None:
    action = args.logs_target
    if action == "recent":
        _run_recent(paths, limit=args.limit, source=args.source, event_type=args.event_type)
        return
    if action == "failures":
        _run_failures(paths, limit=args.limit)
        return
    if action == "trace":
        _run_trace(paths, trace_id=args.trace_id)
        return
    if action == "queue":
        _run_queue(paths, limit=args.limit, status=args.status)
        return
    if action == "session":
        _run_session(paths, session_id=args.session_id, limit=args.limit)
        return
    if action == "chat":
        _run_chat(paths, chat_id=args.chat_id, topic_id=args.topic_id, limit=args.limit)
        return
    raise SystemExit(f"Unsupported logs target: {action}")
