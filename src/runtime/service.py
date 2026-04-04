from __future__ import annotations

from datetime import datetime, timezone
import inspect
import json
import ast
import mimetypes
import queue
import time
import threading
import uuid
import re
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import urlopen

from .debug_mirror import DebugMirror
from core.json_store import load_json, save_json
from core.models import AuthState, CodexServerState, Config, RuntimeState, utc_now
from core.paths import AppPaths
from integrations.telegram import (
    TelegramClient,
    TelegramError,
    describe_pairing,
    has_pending_pairing,
    is_auth_paired,
    is_topic_closed_error,
    register_pairing_request,
)
from setup.setup_flow import complete_pending_pairing
from storage.operations import ServiceRunStore, TraceStore
from storage.db import StorageManager
from storage.log_maintenance import prune_logs
from storage.logging_health import load_logging_health, mark_logging_degraded
from storage.runtime_state_store import (
    load_codex_server_state,
    save_codex_server_state,
    save_runtime_state,
)
from storage.telegram_groups import load_active_message_chunk_ids, sync_message_chunks, upsert_message_group
from storage.telegram_queue import active_delivery_manager, install_delivery_manager, uninstall_delivery_manager
from .app_server_runtime import default_transport_factory, derive_codex_state, is_stale_active_turn, make_app_server_start_fn
from .approval_store import ApprovalStore
from .codex_cli_config import read_codex_cli_preferences, write_codex_cli_preferences
from .control import ServiceConflictChoices, isatty, prepare_service_lock, reset_auth, start_codex_session
from .instructions import ensure_instruction_files
from .performance import (
    PerformanceTracker,
    delivery_manager_supports_background_queue,
    delete_telegram_message,
    edit_telegram_message,
    queue_telegram_delete_message,
    queue_telegram_edit_message,
    queue_telegram_message,
    queue_telegram_typing,
    send_telegram_message,
    send_telegram_typing,
)
from .recorder import Recorder
from .runtime import ServiceRuntime
from .session_store import SessionStore
from .sleep import has_pending_sleep_work, run_sleep, should_run_sleep
from .telegram_html import (
    escape_telegram_html,
    looks_like_telegram_html,
    normalize_legacy_telegram_text,
    repair_partial_telegram_html,
    render_collapsed_thinking_html,
    render_telegram_progress_html,
    to_telegram_html,
)
from .telegram_update_store import TelegramUpdateStore


LOCAL_AUTH_CALLBACK_RE = re.compile(r"https?://(?:localhost|127\.0\.0\.1):1455/auth/callback\?[^\s]+", re.IGNORECASE)
TELEGRAM_TEXT_LIMIT = 4000
TELEGRAM_PARSE_MODE = "HTML"
_AGENT_MESSAGE_PHASES: dict[str, str] = {}
_AGENT_MESSAGE_TEXTS: dict[str, str] = {}
_THINKING_SOURCE_LAST_SENT_AT: dict[str, float] = {}
COMMENTARY_STREAM_MIN_INTERVAL_SECONDS = 1.0
DEFAULT_THINKING_STREAM_MIN_INTERVAL_SECONDS = 1.0
DEFAULT_THINKING_PLACEHOLDER_DELAY_SECONDS = 2.0
_COMMAND_ACTIVITY_PREFIX = "__tele_cli_command__:"
MIN_LIVE_THINKING_LENGTH = 12
THINKING_PLACEHOLDER_SOURCE_KEY = "status:thinking-placeholder"
CODEX_NOTIFICATION_QUEUE_MAX_SIZE = 128
CODEX_NOTIFICATION_POLL_IDLE_SECONDS = 0.02
SERVICE_LOOP_YIELD_SECONDS = 0.01
SERVICE_STARTUP_WAIT_SECONDS = 0.01
SERVICE_THREAD_JOIN_TIMEOUT_SECONDS = 0.05
SERVICE_STARTUP_PRUNE_DELAY_SECONDS = 10.0


def service_tick_seconds(config: Config) -> float:
    configured = max(config.poll_interval_seconds, 0.0)
    if configured == 0.0:
        return 0.05
    return max(min(configured, 0.1), 0.01)


def start_async_log_prune(paths: AppPaths, *, run_id: str | None = None, delay_seconds: float = SERVICE_STARTUP_PRUNE_DELAY_SECONDS) -> threading.Thread:
    def worker() -> None:
        if delay_seconds > 0:
            threading.Event().wait(delay_seconds)
        try:
            prune_logs(paths, run_id=run_id)
        except Exception as exc:
            mark_logging_degraded(paths, operation="prune_logs", error=str(exc), source="storage", event_type="logging.pruned")

    thread = threading.Thread(target=worker, name="log-prune", daemon=True)
    thread.start()
    return thread


def build_app_server_notification_record(method: str, params: dict) -> dict:
    item = params.get("item") if isinstance(params.get("item"), dict) else {}
    return {
        "timestamp": utc_now(),
        "method": method,
        "thread_id": params.get("threadId") or params.get("thread_id"),
        "turn_id": params.get("turnId") or params.get("turn_id"),
        "item_type": item.get("type"),
        "assistant_text": extract_assistant_text(params),
        "thinking_text": extract_thinking_text(params),
        "activity_text": extract_activity_text(method, params),
        "status_text": extract_event_driven_status(method, params),
        "params": params,
    }


def append_telegram_format_failure_log(
    paths: AppPaths,
    *,
    session_id: str,
    trace_id: str | None = None,
    thread_id: str | None,
    turn_id: str | None,
    stage: str,
    error: str,
    raw_text: str,
    rich_text: str,
    escaped_text: str,
    emergency_text: str | None = None,
) -> None:
    record = {
        "timestamp": utc_now(),
        "session_id": session_id,
        "thread_id": thread_id,
        "turn_id": turn_id,
        "stage": stage,
        "error": error,
        "raw_text": raw_text,
        "rich_text": rich_text,
        "escaped_text": escaped_text,
        "emergency_text": emergency_text,
    }
    TraceStore(paths).log_event(
        source="telegram_outbound",
        event_type="telegram.format_failure",
        trace_id=trace_id,
        session_id=session_id,
        thread_id=thread_id,
        turn_id=turn_id,
        payload=record,
    )


def append_telegram_poll_log(paths: AppPaths, event: str, **fields: object) -> None:
    record = {"timestamp": utc_now(), "event": event}
    record.update(fields)
    TraceStore(paths).log_event(
        source="telegram_inbound",
        event_type=f"telegram.poll.{event}",
        payload=record,
    )


def append_recovery_event(
    paths: AppPaths,
    message: str,
    *,
    run_id: str | None = None,
    trace_id: str | None = None,
    session_id: str | None = None,
    thread_id: str | None = None,
    turn_id: str | None = None,
    chat_id: int | None = None,
    topic_id: int | None = None,
) -> None:
    TraceStore(paths, run_id=run_id).log_event(
        source="service",
        event_type="service.recovery",
        trace_id=trace_id,
        session_id=session_id,
        thread_id=thread_id,
        turn_id=turn_id,
        chat_id=chat_id,
        topic_id=topic_id,
        payload={"message": message},
    )


def append_structured_event(
    paths: AppPaths,
    *,
    run_id: str | None = None,
    source: str,
    event_type: str,
    trace_id: str | None = None,
    session_id: str | None = None,
    thread_id: str | None = None,
    turn_id: str | None = None,
    source_event_id: str | None = None,
    chat_id: int | None = None,
    topic_id: int | None = None,
    payload: dict | None = None,
) -> None:
    TraceStore(paths, run_id=run_id).log_event(
        source=source,
        event_type=event_type,
        trace_id=trace_id,
        session_id=session_id,
        thread_id=thread_id,
        turn_id=turn_id,
        source_event_id=source_event_id,
        chat_id=chat_id,
        topic_id=topic_id,
        payload=payload or {},
    )


def remember_agent_message_phase(method: str, params: dict) -> str | None:
    item = params.get("item")
    if isinstance(item, dict) and item.get("type") == "agentMessage":
        item_id = item.get("id")
        phase = item.get("phase")
        if isinstance(item_id, str) and item_id and isinstance(phase, str) and phase:
            _AGENT_MESSAGE_PHASES[item_id] = phase
            if method == "item/started":
                _AGENT_MESSAGE_TEXTS[item_id] = ""
            if method == "item/completed":
                _AGENT_MESSAGE_TEXTS.pop(item_id, None)
            if len(_AGENT_MESSAGE_PHASES) > 256:
                oldest = next(iter(_AGENT_MESSAGE_PHASES))
                _AGENT_MESSAGE_PHASES.pop(oldest, None)
            if len(_AGENT_MESSAGE_TEXTS) > 256:
                oldest = next(iter(_AGENT_MESSAGE_TEXTS))
                _AGENT_MESSAGE_TEXTS.pop(oldest, None)
            return phase
    item_id = params.get("itemId")
    if isinstance(item_id, str) and item_id:
        return _AGENT_MESSAGE_PHASES.get(item_id)
    return None


def accumulate_agent_message_text(method: str, params: dict) -> str | None:
    item_id = params.get("itemId")
    if isinstance(item_id, str) and item_id and method == "item/agentMessage/delta":
        delta = params.get("delta")
        if isinstance(delta, str) and delta:
            combined = f"{_AGENT_MESSAGE_TEXTS.get(item_id, '')}{delta}"
            _AGENT_MESSAGE_TEXTS[item_id] = combined
            return combined
        return _AGENT_MESSAGE_TEXTS.get(item_id)
    item = params.get("item")
    if isinstance(item, dict) and item.get("type") == "agentMessage":
        item_id = item.get("id")
        text = extract_assistant_text(params)
        if isinstance(item_id, str) and item_id and text:
            _AGENT_MESSAGE_TEXTS[item_id] = text
        if method == "item/completed" and isinstance(item_id, str) and item_id:
            _AGENT_MESSAGE_TEXTS.pop(item_id, None)
        return text
    return None


def split_telegram_text(text: str, limit: int | None = None) -> list[str]:
    limit = limit or TELEGRAM_TEXT_LIMIT
    stripped = text.strip()
    if not stripped:
        return []
    chunks: list[str] = []
    remaining = stripped
    while len(remaining) > limit:
        split_at = remaining.rfind("\n\n", 0, limit + 1)
        if split_at <= 0:
            split_at = remaining.rfind("\n", 0, limit + 1)
        if split_at <= 0:
            split_at = remaining.rfind(" ", 0, limit + 1)
        if split_at <= 0:
            split_at = limit
        chunk = remaining[:split_at].strip()
        if not chunk:
            chunk = remaining[:limit].strip()
            split_at = limit
        chunks.append(chunk)
        remaining = remaining[split_at:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks


def _streaming_message_ids(session) -> list[int]:
    ids = [message_id for message_id in getattr(session, "streaming_message_ids", []) if isinstance(message_id, int)]
    if not ids and isinstance(session.streaming_message_id, int):
        ids = [session.streaming_message_id]
    return ids


def _set_streaming_message_ids(session, message_ids: list[int]) -> None:
    session.streaming_message_ids = [message_id for message_id in message_ids if isinstance(message_id, int)]
    session.streaming_message_id = session.streaming_message_ids[0] if session.streaming_message_ids else None


def _split_telegram_html_text(text: str, limit: int | None = None) -> list[str]:
    limit = limit or TELEGRAM_TEXT_LIMIT
    normalized = (text or "").strip()
    if not normalized:
        return []
    if len(normalized) <= limit:
        return [normalized]

    token_pattern = re.compile(r"(<[^>]+>)")
    tokens = [token for token in token_pattern.split(normalized) if token]
    chunks: list[str] = []
    current = ""
    open_tags: list[tuple[str, str]] = []

    def closing_markup() -> str:
        return "".join(f"</{name}>" for name, _ in reversed(open_tags))

    def reopening_markup() -> str:
        return "".join(tag for _, tag in open_tags)

    def flush_current() -> None:
        nonlocal current
        body = current.strip()
        if not body:
            current = reopening_markup()
            return
        chunk = f"{body}{closing_markup()}".strip()
        if chunk:
            chunks.append(chunk)
        current = reopening_markup()

    def register_tag(tag: str) -> None:
        stripped = tag.strip()
        closing = re.match(r"^</\s*([a-zA-Z0-9-]+)\s*>$", stripped)
        if closing:
            name = closing.group(1).lower()
            for index in range(len(open_tags) - 1, -1, -1):
                if open_tags[index][0] == name:
                    del open_tags[index]
                    break
            return
        opening = re.match(r"^<\s*([a-zA-Z0-9-]+)\b[^>]*?>$", stripped)
        if not opening or stripped.endswith("/>"):
            return
        name = opening.group(1).lower()
        open_tags.append((name, tag))

    for token in tokens:
        if token.startswith("<") and token.endswith(">"):
            projected = current + token + closing_markup()
            if len(projected) > limit and current.strip():
                flush_current()
            current += token
            register_tag(token)
            continue

        remaining = token
        while remaining:
            projected = current + remaining + closing_markup()
            if len(projected) <= limit:
                current += remaining
                remaining = ""
                continue
            available = limit - len(current) - len(closing_markup())
            if available <= 0:
                flush_current()
                continue
            split_at = remaining.rfind("\n\n", 0, available + 1)
            if split_at <= 0:
                split_at = remaining.rfind("\n", 0, available + 1)
            if split_at <= 0:
                split_at = remaining.rfind(" ", 0, available + 1)
            if split_at <= 0:
                split_at = available
            segment = remaining[:split_at]
            if not segment:
                segment = remaining[:available]
                split_at = len(segment)
            current += segment
            remaining = remaining[split_at:]
            flush_current()
            remaining = remaining.lstrip()

    if current.strip():
        flush_current()
    return [chunk for chunk in chunks if chunk.strip()]


def _sync_telegram_message_chunks(
    paths: AppPaths,
    telegram: TelegramClient,
    chat_id: int,
    *,
    session,
    rendered_chunks: list[str],
    topic_id: int | None,
    parse_mode: str | None,
    disable_notification: bool,
    queue_only: bool = False,
    performance: PerformanceTracker | None = None,
    context: dict | None = None,
) -> None:
    context = dict(context or {})
    context.pop("performance", None)
    context.setdefault("session_id", session.session_id)
    context.setdefault("trace_id", getattr(session, "current_trace_id", None))
    context.setdefault("thread_id", session.thread_id)
    context.setdefault("turn_id", session.active_turn_id or session.last_completed_turn_id)
    message_group_id = _message_group_id_for_session(session, context)
    context.setdefault("message_group_id", message_group_id)
    existing_ids = _streaming_message_ids(session)
    logical_role = _logical_role_from_context(context)
    if not existing_ids:
        existing_ids = _stored_streaming_message_ids(paths, session=session, logical_role=logical_role)
    if not existing_ids and logical_role != "live_progress":
        existing_ids = _stored_streaming_message_ids(paths, session=session, logical_role="live_progress")
    kept_ids: list[int] = []
    delivery_manager = active_delivery_manager()

    for index, chunk in enumerate(rendered_chunks):
        if parse_mode == TELEGRAM_PARSE_MODE:
            chunk = repair_partial_telegram_html(chunk)
        dedupe_key = _message_chunk_dedupe_key(message_group_id, index)
        message_id = existing_ids[index] if index < len(existing_ids) else None
        if not isinstance(message_id, int) and delivery_manager is not None:
            message_id = delivery_manager.latest_message_id_for_dedupe(dedupe_key)
        if isinstance(message_id, int):
            if queue_only:
                queue_telegram_edit_message(
                    chat_id,
                    message_id,
                    chunk,
                    parse_mode=parse_mode,
                    performance=performance,
                    dedupe_key=dedupe_key,
                    **context,
                )
            else:
                allow_paused_return = delivery_manager.is_paused() if delivery_manager is not None else False
                edit_telegram_message(
                    telegram,
                    chat_id,
                    message_id,
                    chunk,
                    parse_mode=parse_mode,
                    allow_paused_return=allow_paused_return,
                    performance=performance,
                    dedupe_key=dedupe_key,
                    **context,
                )
            kept_ids.append(message_id)
            continue
        if queue_only:
            message_id = queue_telegram_message(
                chat_id,
                chunk,
                topic_id=topic_id,
                parse_mode=parse_mode,
                disable_notification=disable_notification,
                performance=performance,
                dedupe_key=dedupe_key,
                **context,
            )
            if isinstance(message_id, int):
                kept_ids.append(message_id)
        else:
            allow_paused_return = delivery_manager.is_paused() if delivery_manager is not None else False
            message_id = send_telegram_message(
                telegram,
                chat_id,
                chunk,
                topic_id=topic_id,
                parse_mode=parse_mode,
                disable_notification=disable_notification,
                allow_paused_return=allow_paused_return,
                performance=performance,
                dedupe_key=dedupe_key,
                **context,
            )
            if isinstance(message_id, int):
                kept_ids.append(message_id)

    for message_id in existing_ids[len(rendered_chunks) :]:
        try:
            if queue_only:
                queue_telegram_delete_message(
                    chat_id,
                    message_id,
                    performance=performance,
                    dedupe_key=f"{message_group_id}:delete:{message_id}",
                    **context,
                )
            else:
                allow_paused_return = delivery_manager.is_paused() if delivery_manager is not None else False
                delete_telegram_message(
                    telegram,
                    chat_id,
                    message_id,
                    allow_paused_return=allow_paused_return,
                    performance=performance,
                    dedupe_key=f"{message_group_id}:delete:{message_id}",
                    **context,
                )
        except TelegramError:
            pass

    _set_streaming_message_ids(session, kept_ids)
    if chat_id:
        upsert_message_group(
            paths,
            message_group_id=message_group_id,
            session_id=session.session_id,
            trace_id=getattr(session, "current_trace_id", None),
            chat_id=chat_id,
            topic_id=topic_id,
            logical_role=_logical_role_from_context(context),
            status="active" if _logical_role_from_context(context) == "live_progress" else "finalized",
            finalized=_logical_role_from_context(context) != "live_progress",
        )
        sync_message_chunks(paths, message_group_id=message_group_id, rendered_chunks=rendered_chunks, telegram_message_ids=kept_ids)


def _clear_streaming_messages(telegram: TelegramClient | None, chat_id: int | None, session) -> None:
    if not chat_id:
        _set_streaming_message_ids(session, [])
        return
    if telegram is None:
        _set_streaming_message_ids(session, [])
        return
    for message_id in _streaming_message_ids(session):
        try:
            delete_telegram_message(telegram, chat_id, message_id, allow_paused_return=True)
        except Exception:
            pass
    _set_streaming_message_ids(session, [])


def _has_completed_visible_answer(session) -> bool:
    delivered = (session.last_delivered_output_text or "").strip()
    streaming = (session.streaming_output_text or "").strip()
    return bool(delivered and streaming and delivered == streaming and session.streaming_phase == "answer")


def _has_preservable_visible_answer(session) -> bool:
    if _has_completed_visible_answer(session):
        return True
    if session.status != "DELIVERING_FINAL":
        return False
    if not _streaming_message_ids(session):
        return False
    return bool((session.streaming_output_text or "").strip() or (session.pending_output_text or "").strip())


def _should_queue_follow_up_user_message(session) -> bool:
    if not session.active_turn_id:
        return False
    if is_stale_active_turn(session):
        return False
    if session.status == "DELIVERING_FINAL":
        return True
    if session.streaming_phase in {"answer", "finalizing"}:
        return True
    return False


def _queue_follow_up_user_message(session_store: SessionStore, session, text: str) -> None:
    queued = text.strip()
    if not queued:
        return
    existing = session.queued_user_input_text.strip()
    session.queued_user_input_text = f"{existing}\n\n{queued}" if existing else queued
    session.last_user_message_at = utc_now()
    session_store.save_session(session)


def _build_final_rendered_chunks(*, answer_html: str, thinking_html: str) -> list[str]:
    answer_chunks = _split_telegram_html_text(answer_html)
    if not thinking_html:
        return answer_chunks
    if not answer_chunks:
        return [thinking_html]
    if len(thinking_html) + 2 + len(answer_chunks[0]) <= TELEGRAM_TEXT_LIMIT:
        return [f"{thinking_html}\n\n{answer_chunks[0]}"] + answer_chunks[1:]
    return [thinking_html] + answer_chunks


def _logical_role_from_context(context: dict | None) -> str:
    category = str((context or {}).get("category") or "")
    if category == "thinking_output":
        return "live_progress"
    if category == "error":
        return "error_output"
    return "final_output"


def _live_progress_trace_token(session) -> str:
    base = getattr(session, "current_trace_id", None) or session.active_turn_id or session.last_completed_turn_id or "session"
    timestamp = (session.last_user_message_at or "").strip()
    if not timestamp:
        return str(base)
    return f"{base}:{timestamp}"


def _message_group_id_for_session(session, context: dict | None) -> str:
    logical_role = _logical_role_from_context(context)
    trace_token = (
        _live_progress_trace_token(session)
        if logical_role == "live_progress"
        else getattr(session, "current_trace_id", None) or session.active_turn_id or session.last_completed_turn_id or "session"
    )
    return f"{session.session_id}:{logical_role}:{trace_token}"


def _message_chunk_dedupe_key(message_group_id: str, chunk_index: int) -> str:
    return f"{message_group_id}:chunk:{chunk_index}"


def _message_group_id_for_role(session, logical_role: str) -> str:
    trace_token = (
        _live_progress_trace_token(session)
        if logical_role == "live_progress"
        else getattr(session, "current_trace_id", None) or session.active_turn_id or session.last_completed_turn_id or "session"
    )
    return f"{session.session_id}:{logical_role}:{trace_token}"


def _stored_streaming_message_ids(paths: AppPaths, *, session, logical_role: str) -> list[int]:
    return load_active_message_chunk_ids(paths, message_group_id=_message_group_id_for_role(session, logical_role))


def _message_group_queue_state(paths: AppPaths, *, message_group_id: str) -> dict[str, int]:
    storage = StorageManager(paths)
    with storage.read_connection() as connection:
        row = connection.execute(
            """
            SELECT
                COUNT(*) AS total_count,
                SUM(CASE WHEN status = 'queued' THEN 1 ELSE 0 END) AS queued_count,
                SUM(CASE WHEN status = 'claimed' THEN 1 ELSE 0 END) AS claimed_count,
                SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) AS completed_count,
                SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed_count
            FROM telegram_outbound_queue
            WHERE message_group_id = ?
            """,
            (message_group_id,),
        ).fetchone()
    if row is None:
        return {
            "total_count": 0,
            "queued_count": 0,
            "claimed_count": 0,
            "completed_count": 0,
            "failed_count": 0,
        }
    return {key: int(row[key] or 0) for key in row.keys()}


def _message_group_failed_errors(paths: AppPaths, *, message_group_id: str) -> list[str]:
    storage = StorageManager(paths)
    with storage.read_connection() as connection:
        rows = connection.execute(
            """
            SELECT last_error
            FROM telegram_outbound_queue
            WHERE message_group_id = ? AND status = 'failed'
            ORDER BY created_at, queue_id
            """,
            (message_group_id,),
        ).fetchall()
    return [str(row["last_error"] or "") for row in rows]


def _delete_failed_message_group_rows(paths: AppPaths, *, message_group_id: str) -> None:
    storage = StorageManager(paths)
    with storage.transaction() as connection:
        connection.execute(
            """
            DELETE FROM telegram_outbound_queue
            WHERE message_group_id = ? AND status = 'failed'
            """,
            (message_group_id,),
        )


def _abandon_failed_final_delivery(
    paths: AppPaths,
    session_store: SessionStore,
    session,
    *,
    reason: str,
) -> None:
    append_recovery_event(
        paths,
        f"final_delivery_abandoned session_id={session.session_id} reason={reason}",
        trace_id=getattr(session, "current_trace_id", None),
        session_id=session.session_id,
        thread_id=session.thread_id,
        turn_id=session.last_completed_turn_id,
        chat_id=session.transport_chat_id,
        topic_id=session.transport_topic_id,
    )
    session.attached = False
    session.status = "ACTIVE"
    session.pending_output_text = ""
    session.pending_output_updated_at = None
    session.streaming_output_text = ""
    session.streaming_phase = ""
    session.streaming_message_id = None
    session.streaming_message_ids = []
    session.thinking_message_id = None
    session.thinking_message_ids = []
    session.thinking_live_message_ids = {}
    session.thinking_live_texts = {}
    session.thinking_sent_texts = {}
    session.thinking_message_text = ""
    session.thinking_history_text = ""
    session.thinking_history_order = []
    session.thinking_history_by_source = {}
    session.last_thinking_sent_text = ""
    session.current_trace_id = None
    session_store.save_session(session)
    pruned = session_store.prune_detached_sessions()
    if pruned:
        append_recovery_event(paths, f"detached_sessions_pruned count={pruned}")


def _cancel_queued_live_progress_operations(paths: AppPaths, session, *, include_typing: bool) -> None:
    session_id = getattr(session, "session_id", None)
    if not isinstance(session_id, str) or not session_id:
        return
    storage = StorageManager(paths)
    with storage.transaction() as connection:
        connection.execute(
            """
            DELETE FROM telegram_outbound_queue
            WHERE status = 'queued'
              AND session_id = ?
              AND message_group_id LIKE ?
            """,
            (session_id, f"{session_id}:live_progress:%"),
        )
        if include_typing:
            connection.execute(
                """
                DELETE FROM telegram_outbound_queue
                WHERE status = 'queued'
                  AND session_id = ?
                  AND op_type = 'typing'
                """,
                (session_id,),
            )


def reconcile_pending_final_deliveries(
    paths: AppPaths,
    auth: AuthState,
    telegram: TelegramClient,
    recorder: Recorder,
    session_store: SessionStore,
    *,
    performance: PerformanceTracker | None = None,
) -> None:
    for session in session_store.list_all_telegram_sessions():
        if session.status != "DELIVERING_FINAL":
            continue
        final_text = session.pending_output_text.strip() or session.streaming_output_text.strip()
        if not final_text:
            continue
        message_group_id = _message_group_id_for_role(session, "final_output")
        queue_state = _message_group_queue_state(paths, message_group_id=message_group_id)
        if queue_state["queued_count"] or queue_state["claimed_count"]:
            continue
        if queue_state["failed_count"]:
            failed_errors = _message_group_failed_errors(paths, message_group_id=message_group_id)
            if any(is_topic_closed_error(TelegramError(error)) for error in failed_errors):
                _delete_failed_message_group_rows(paths, message_group_id=message_group_id)
                if session.transport_topic_id is not None:
                    append_recovery_event(
                        paths,
                        (
                            "final_delivery_retrying_without_topic "
                            f"session_id={session.session_id} topic_id={session.transport_topic_id}"
                        ),
                        trace_id=getattr(session, "current_trace_id", None),
                        session_id=session.session_id,
                        thread_id=session.thread_id,
                        turn_id=session.last_completed_turn_id,
                        chat_id=session.transport_chat_id,
                        topic_id=session.transport_topic_id,
                    )
                    session.transport_topic_id = None
                    session_store.save_session(session)
                    flush_buffer(
                        session.session_id,
                        auth,
                        telegram,
                        recorder,
                        session_store,
                        mark_agent=True,
                        stream_format=True,
                        queue_only=True,
                        performance=performance,
                    )
                else:
                    _abandon_failed_final_delivery(
                        paths,
                        session_store,
                        session,
                        reason="topic_closed_without_recoverable_topic",
                    )
                continue
            append_recovery_event(
                paths,
                (
                    "final_delivery_waiting_for_retry "
                    f"session_id={session.session_id} failed_count={queue_state['failed_count']}"
                ),
                trace_id=getattr(session, "current_trace_id", None),
                session_id=session.session_id,
                thread_id=session.thread_id,
                turn_id=session.last_completed_turn_id,
                chat_id=session.transport_chat_id,
                topic_id=session.transport_topic_id,
            )
            continue
        if queue_state["total_count"] <= 0 or queue_state["completed_count"] <= 0:
            continue
        clear_thinking_message(auth, telegram, session_store, session, performance=performance)
        recorder.record("assistant", final_text)
        session.streaming_message_id = None
        session.streaming_message_ids = []
        session.streaming_output_text = ""
        session.streaming_phase = ""
        session.last_thinking_sent_text = ""
        session.thinking_history_text = ""
        session.thinking_history_order = []
        session.thinking_history_by_source = {}
        session.thinking_message_text = ""
        session.thinking_message_ids = []
        session.thinking_live_message_ids = {}
        session.thinking_live_texts = {}
        session.thinking_sent_texts = {}
        session.status = "ACTIVE"
        session_store.mark_delivered_output(session, final_text)
        session_store.mark_agent_message(session)
        session_store.consume_pending_output(session)
        session_store.save_session(session)
        pruned = session_store.prune_detached_sessions()
        if pruned:
            append_recovery_event(paths, f"detached_sessions_pruned count={pruned}")


def dispatch_queued_user_inputs(
    paths: AppPaths,
    auth: AuthState,
    runtime_state: RuntimeState,
    telegram: TelegramClient,
    recorder: Recorder,
    session_store: SessionStore,
    codex,
    *,
    config: Config | None = None,
    performance: PerformanceTracker | None = None,
) -> None:
    if codex is None:
        return
    for session in session_store.list_telegram_sessions(auth):
        queued_text = session.queued_user_input_text.strip()
        if not queued_text:
            continue
        if not session.attached or session.status != "ACTIVE" or session.active_turn_id:
            continue
        session.queued_user_input_text = ""
        session_store.save_session(session)
        append_structured_event(
            paths,
            run_id=runtime_state.session_id,
            source="service",
            event_type="ai.request.dequeued",
            session_id=session.session_id,
            thread_id=session.thread_id,
            turn_id=session.last_completed_turn_id,
            chat_id=session.transport_chat_id or auth.telegram_chat_id,
            topic_id=session.transport_topic_id,
            payload={"text_preview": queued_text[:160]},
        )
        handle_authorized_message(
            queued_text,
            scoped_auth_for_update(
                auth,
                chat_id=session.transport_chat_id,
                user_id=session.transport_user_id,
            ),
            runtime_state,
            codex,
            telegram,
            recorder,
            session_store,
            session.transport_topic_id,
            performance,
            paths=paths,
            config=config,
            source_event_id=None,
            visible_topic_name=session.visible_topic_name,
        )


def start_telegram_polling_thread(
    *,
    paths: AppPaths,
    config: Config,
    telegram: TelegramClient,
    runtime_state: RuntimeState,
    update_queue: queue.Queue[dict],
    stop_event: threading.Event,
    poll_gate: threading.Event,
) -> threading.Thread:
    def worker() -> None:
        offset: int | None = None
        telegram_failures = 0
        next_poll_at = 0.0
        append_telegram_poll_log(paths, "thread_started")
        while not stop_event.is_set():
            try:
                if not poll_gate.is_set() or not update_queue.empty():
                    stop_event.wait(0.01)
                    continue
                now = time.monotonic()
                if now < next_poll_at:
                    stop_event.wait(min(next_poll_at - now, 0.1))
                    continue
                updates = telegram.get_updates(offset=offset, timeout=20)
                append_telegram_poll_log(
                    paths,
                    "poll_result",
                    offset=offset,
                    update_count=len(updates),
                )
                if runtime_state.telegram_state != "RUNNING":
                    runtime_state.telegram_state = "RUNNING"
                    save_runtime_state(paths, runtime_state)
                telegram_failures = 0
                next_poll_at = 0.0
            except TelegramError as exc:
                telegram_failures += 1
                delay = telegram_retry_delay(config, telegram_failures)
                next_poll_at = time.monotonic() + delay
                runtime_state.telegram_state = "BACKOFF"
                save_runtime_state(paths, runtime_state)
                append_recovery_event(
                    paths,
                    f"telegram poll failed -> backoff={delay:.1f}s error={exc}",
                )
                append_telegram_poll_log(paths, "poll_error", error=str(exc), backoff_seconds=delay)
                continue
            except Exception as exc:
                telegram_failures += 1
                delay = telegram_unexpected_retry_delay(config, telegram_failures)
                next_poll_at = time.monotonic() + delay
                runtime_state.telegram_state = "BACKOFF"
                save_runtime_state(paths, runtime_state)
                append_recovery_event(
                    paths,
                    f"telegram poll crashed -> backoff={delay:.1f}s error={exc}",
                )
                append_telegram_poll_log(
                    paths,
                    "poll_crash",
                    error=repr(exc),
                    error_type=type(exc).__name__,
                    backoff_seconds=delay,
                )
                continue
            for update in updates:
                update_id = update.get("update_id")
                if isinstance(update_id, int):
                    offset = update_id + 1
                append_telegram_poll_log(paths, "update_enqueued", update_id=update_id, next_offset=offset)
                update_queue.put(update)

    thread = threading.Thread(target=worker, name="telegram-poll", daemon=True)
    thread.start()
    return thread


def invoke_start_codex_session_fn(
    start_codex_session_fn,
    config: Config,
    auth: AuthState,
    runtime: ServiceRuntime,
    runtime_state: RuntimeState,
    metadata,
    app_lock,
    telegram: TelegramClient,
    handle_output,
    performance: PerformanceTracker | None = None,
):
    try:
        parameters = inspect.signature(start_codex_session_fn).parameters.values()
    except (TypeError, ValueError):
        parameters = ()
    supports_performance = any(param.kind == inspect.Parameter.VAR_POSITIONAL for param in parameters) or len(
        list(parameters)
    ) >= 9
    if supports_performance:
        return start_codex_session_fn(
            config,
            auth,
            runtime,
            runtime_state,
            metadata,
            app_lock,
            telegram,
            handle_output,
            performance,
        )
    return start_codex_session_fn(
        config,
        auth,
        runtime,
        runtime_state,
        metadata,
        app_lock,
        telegram,
        handle_output,
    )


def build_status_message(
    auth: AuthState,
    runtime_state: RuntimeState,
    session_store: SessionStore | None = None,
    topic_id: int | None = None,
    model: str | None = None,
    reasoning: str | None = None,
) -> str:
    logging_state = load_logging_health(session_store.paths) if session_store is not None else {"state": "healthy"}
    session_lines: list[str] = []
    if session_store is not None and auth.telegram_chat_id:
        sessions = session_store.list_telegram_sessions(auth, topic_id)
        active = session_store.get_current_telegram_session(auth, topic_id)
        session_lines.extend(
            [
                f"sessions={len(sessions)}",
                f"active_session={active.session_id if active else 'none'}",
                f"active_session_status={active.status if active else 'none'}",
                f"active_thread={active.thread_id if active and active.thread_id else 'none'}",
                f"active_turn={active.active_turn_id if active and active.active_turn_id else 'none'}",
            ]
        )
    approval_lines: list[str] = []
    if auth.telegram_chat_id and session_store is not None:
        pending_approvals = ApprovalStore(session_store.paths).pending()
        stale_approvals = ApprovalStore(session_store.paths).stale()
        approval_lines.append(f"pending_approvals={len(pending_approvals)}")
        approval_lines.append(f"stale_approvals={len(stale_approvals)}")
    return (
        "Tele Cli status\n"
        f"service={runtime_state.service_state}\n"
        f"telegram={runtime_state.telegram_state}\n"
        f"codex={runtime_state.codex_state}\n"
        f"logging={'DEGRADED' if logging_state.get('state') == 'degraded' else 'HEALTHY'}\n"
        f"pairing={describe_pairing(auth)}"
        + (f"\nmodel={model}" if model else "")
        + (f"\nreasoning={reasoning}" if reasoning else "")
        + (("\n" + "\n".join(session_lines + approval_lines)) if (session_lines or approval_lines) else "")
    )


def session_log_label(session) -> str:
    return (
        f"session_id={session.session_id} "
        f"attached={session.attached} "
        f"status={session.status} "
        f"thread_id={session.thread_id or 'none'} "
        f"turn_id={session.active_turn_id or 'none'}"
    )


def get_latest_user_session(
    session_store: SessionStore,
    auth: AuthState,
    *,
    require_active_turn: bool = False,
):
    if not auth.telegram_user_id:
        return None
    sessions = [
        session
        for session in session_store.load().sessions
        if session.transport == "telegram"
        and session.transport_user_id == auth.telegram_user_id
        and session.attached
        and session_store.is_recoverable(session)
        and (not require_active_turn or bool(session.active_turn_id))
    ]
    if not sessions:
        return None

    def sort_key(session) -> tuple[str, str]:
        return (
            session.last_user_message_at or "",
            session.last_agent_message_at or "",
        )

    return max(sessions, key=sort_key)


def scoped_auth_for_update(auth: AuthState, *, chat_id: int | None, user_id: int | None) -> AuthState:
    scoped = AuthState.from_dict(auth.to_dict())
    if chat_id is not None:
        scoped.telegram_chat_id = chat_id
    if user_id is not None:
        scoped.telegram_user_id = user_id
    return scoped


def parse_request_command(text: str, command: str) -> int | None:
    prefix = f"{command} "
    if not text.startswith(prefix):
        return None
    try:
        return int(text[len(prefix) :].strip())
    except ValueError:
        return None


def parse_value_command(text: str, command: str) -> str | None:
    prefix = f"{command} "
    if not text.startswith(prefix):
        return None
    value = text[len(prefix) :].strip()
    return value or None


def restart_status_text(value: str, label: str, restarted) -> str:
    suffix = "Codex runtime restarted." if restarted is not None else "Codex restart is pending."
    return f'{label} set to "{value}". {suffix}'


def stop_codex_runtime(codex) -> None:
    if codex is None or not hasattr(codex, "stop"):
        return
    try:
        codex.stop()
    except Exception:
        pass


def restart_codex_runtime(
    *,
    paths: AppPaths,
    config: Config,
    auth: AuthState,
    runtime: ServiceRuntime,
    runtime_state: RuntimeState,
    metadata,
    app_lock,
    telegram: TelegramClient,
    handle_output,
    codex,
    start_codex_session_fn,
    performance: PerformanceTracker | None = None,
):
    stop_codex_runtime(codex)
    runtime.set_codex_state("STOPPED")
    save_runtime_state(paths, runtime_state)
    restarted = None
    if is_auth_paired(auth):
        restarted = invoke_start_codex_session_fn(
            start_codex_session_fn,
            config,
            auth,
            runtime,
            runtime_state,
            metadata,
            app_lock,
            telegram,
            handle_output,
            performance,
        )
    return restarted


def reset_session_after_request_failure(
    session_store: SessionStore | None,
    auth: AuthState,
    *,
    telegram: TelegramClient | None = None,
    session=None,
    topic_id: int | None,
    error_text: str,
    clear_messages: bool = True,
) -> None:
    if session_store is None:
        return
    if session is None:
        session = session_store.get_current_telegram_session(auth, topic_id)
    if session is None:
        return
    if clear_messages:
        _clear_streaming_messages(telegram, session.transport_chat_id or auth.telegram_chat_id, session)
    else:
        _set_streaming_message_ids(session, [])
    session.active_turn_id = None
    session.pending_output_text = ""
    session.pending_output_updated_at = None
    session.streaming_message_id = None
    session.streaming_message_ids = []
    session.thinking_message_id = None
    session.thinking_message_ids = []
    session.thinking_live_message_ids = {}
    session.thinking_live_texts = {}
    session.thinking_sent_texts = {}
    session.thinking_history_order = []
    session.thinking_history_by_source = {}
    session.streaming_output_text = ""
    session.streaming_phase = ""
    session.thinking_message_text = ""
    session.thinking_history_text = ""
    session.last_thinking_sent_text = ""
    session.status = "ACTIVE"
    if "threadId" in error_text or "thread id" in error_text.lower():
        session.thread_id = None
        session.last_completed_turn_id = None
    session_store.save_session(session)


def log_request_failure(
    trace_store: TraceStore | None,
    session_store: SessionStore | None,
    auth: AuthState,
    topic_id: int | None,
    error_text: str,
    *,
    session=None,
) -> None:
    if trace_store is None or session_store is None:
        return
    failed_session = session
    if failed_session is None:
        failed_session = session_store.get_current_telegram_session(auth, topic_id)
    if failed_session is None or not getattr(failed_session, "current_trace_id", None):
        return
    trace_store.log_event(
        source="service",
        event_type="ai.request.failed",
        trace_id=failed_session.current_trace_id,
        session_id=failed_session.session_id,
        thread_id=failed_session.thread_id,
        turn_id=failed_session.active_turn_id,
        chat_id=failed_session.transport_chat_id or auth.telegram_chat_id,
        topic_id=failed_session.transport_topic_id,
        payload={"error": error_text},
    )


def _render_codex_error_html(error_text: str) -> str:
    return (
        "<pre><code>"
        f"{escape_telegram_html(render_codex_error_message(error_text))}"
        "</code></pre>"
    )


def codex_login_required(error_text: str, codex_state: CodexServerState | None = None) -> bool:
    if codex_state is not None and codex_state.auth_required:
        return True
    normalized = render_codex_error_message(error_text).lower()
    return "401 unauthorized" in normalized or "missing bearer or basic authentication" in normalized


def build_codex_login_required_message(codex_state: CodexServerState | None = None) -> str:
    lines = ["Codex login is required before I can reply."]
    if codex_state is not None and codex_state.login_url:
        lines.extend(
            [
                "",
                "Open this URL and finish sign-in:",
                codex_state.login_url,
                "",
                "When the browser lands on a localhost callback URL, paste that full URL into this chat to finish login.",
            ]
        )
    lines.extend(
        [
            "",
            "Or log in manually on the device with:",
            "codex login --device-auth",
        ]
    )
    return "\n".join(lines)


def _render_codex_auth_required_html(codex_state: CodexServerState | None = None) -> str:
    return escape_telegram_html(build_codex_login_required_message(codex_state))


def extract_codex_error_text(params: dict) -> str | None:
    error = params.get("error")
    if isinstance(error, dict):
        message = error.get("message")
        if isinstance(message, str) and message.strip():
            return message.strip()
    turn = params.get("turn")
    if isinstance(turn, dict):
        turn_error = turn.get("error")
        if isinstance(turn_error, dict):
            message = turn_error.get("message")
            if isinstance(message, str) and message.strip():
                return message.strip()
    result = params.get("result")
    if isinstance(result, dict):
        result_error = result.get("error")
        if isinstance(result_error, dict):
            message = result_error.get("message")
            if isinstance(message, str) and message.strip():
                return message.strip()
    return None


def turn_completed_with_error(params: dict) -> bool:
    if extract_codex_error_text(params):
        return True
    turn = params.get("turn")
    if not isinstance(turn, dict):
        return False
    status = str(turn.get("status") or "").strip().lower()
    return status == "failed"


def render_codex_error_message(error_text: str) -> str:
    normalized = str(error_text or "").strip()
    if not normalized:
        return "The request failed."
    try:
        payload = ast.literal_eval(normalized)
    except (SyntaxError, ValueError):
        payload = None
    if isinstance(payload, dict):
        message = payload.get("message")
        if isinstance(message, str) and message.strip():
            return message.strip()
        inner = payload.get("error")
        if isinstance(inner, dict):
            message = inner.get("message")
            if isinstance(message, str) and message.strip():
                return message.strip()
    return normalized


def publish_codex_request_error(
    session_store: SessionStore | None,
    auth: AuthState,
    telegram: TelegramClient,
    *,
    session=None,
    topic_id: int | None,
    error_text: str,
    performance: PerformanceTracker | None = None,
) -> None:
    if session is None and session_store is not None:
        session = session_store.get_current_telegram_session(auth, topic_id)
    target_chat_id = session.transport_chat_id if session is not None else auth.telegram_chat_id
    if target_chat_id is None:
        target_chat_id = auth.telegram_chat_id
    if not target_chat_id:
        return
    codex_state = load_codex_server_state(session_store.paths) if session_store is not None else None
    login_required = codex_login_required(error_text, codex_state)
    rendered_error_html = (
        _render_codex_auth_required_html(codex_state)
        if login_required
        else _render_codex_error_html(error_text)
    )
    rendered_error_text = (
        build_codex_login_required_message(codex_state)
        if login_required
        else render_codex_error_message(error_text)
    )
    live_html = _render_live_thinking_html(session).strip() if session is not None else ""
    context = {
        "performance": performance,
        "category": "error",
        "session_id": session.session_id if session is not None else None,
        "thread_id": session.thread_id if session is not None else None,
        "turn_id": session.active_turn_id if session is not None else None,
    }
    if session is not None and _streaming_message_ids(session) and live_html:
        rendered_html = f"{live_html}\n\n{rendered_error_html}"
        _sync_telegram_message_chunks(
            session_store.paths,
            telegram,
            target_chat_id,
            session=session,
            rendered_chunks=_split_telegram_html_text(rendered_html),
            topic_id=session.transport_topic_id,
            parse_mode=TELEGRAM_PARSE_MODE,
            disable_notification=False,
            performance=performance,
            context=context,
        )
        return
    send_telegram_message(
        telegram,
        target_chat_id,
        rendered_error_text,
        topic_id=session.transport_topic_id if session is not None else topic_id,
        performance=performance,
        category="error",
        session_id=session.session_id if session is not None else None,
        thread_id=session.thread_id if session is not None else None,
        turn_id=session.active_turn_id if session is not None else None,
    )


def extract_update_topic_id(update: dict) -> int | None:
    message = update.get("message") or {}
    topic_id = message.get("message_thread_id")
    return int(topic_id) if isinstance(topic_id, int) else None


def extract_update_topic_name(update: dict) -> str | None:
    message = update.get("message") or {}
    candidates = [
        ((message.get("forum_topic_created") or {}).get("name") if isinstance(message.get("forum_topic_created"), dict) else None),
        ((message.get("forum_topic_edited") or {}).get("name") if isinstance(message.get("forum_topic_edited"), dict) else None),
        (
            ((message.get("reply_to_message") or {}).get("forum_topic_created") or {}).get("name")
            if isinstance((message.get("reply_to_message") or {}).get("forum_topic_created"), dict)
            else None
        ),
        (
            ((message.get("reply_to_message") or {}).get("forum_topic_edited") or {}).get("name")
            if isinstance((message.get("reply_to_message") or {}).get("forum_topic_edited"), dict)
            else None
        ),
    ]
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    topic_id = extract_update_topic_id(update)
    if topic_id is not None:
        return f"topic-{topic_id}"
    return None


def telegram_media_dir(paths: AppPaths):
    directory = paths.root / "telegram_media"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def sanitize_telegram_filename(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", name.strip())
    return cleaned or "attachment"


def save_telegram_file(
    paths: AppPaths,
    telegram: TelegramClient,
    *,
    file_id: str,
    suggested_name: str,
    trace_store: TraceStore | None = None,
    source_event_id: str | None = None,
    chat_id: int | None = None,
    topic_id: int | None = None,
) -> str:
    try:
        metadata = telegram.get_file(file_id)
        file_path = str(metadata.get("file_path") or "").strip()
        if not file_path:
            raise TelegramError(f"Telegram file metadata missing file_path for {file_id}")
        content = telegram.download_file(file_path)
        destination = telegram_media_dir(paths) / f"{utc_now().replace(':', '').replace('-', '')}_{sanitize_telegram_filename(suggested_name)}"
        destination.write_bytes(content)
        relative_path = destination.relative_to(paths.root).as_posix()
        if trace_store is not None:
            trace_store.log_event(
                source="telegram_inbound",
                event_type="telegram.attachment.saved",
                source_event_id=source_event_id,
                chat_id=chat_id,
                topic_id=topic_id,
                payload={
                    "file_id": file_id,
                    "suggested_name": suggested_name,
                    "file_path": file_path,
                    "saved_relpath": relative_path,
                    "size_bytes": len(content),
                },
            )
        return relative_path
    except Exception as exc:
        if trace_store is not None:
            trace_store.log_event(
                source="telegram_inbound",
                event_type="telegram.attachment.failed",
                source_event_id=source_event_id,
                chat_id=chat_id,
                topic_id=topic_id,
                payload={"file_id": file_id, "suggested_name": suggested_name, "error": str(exc)},
            )
        raise


def build_telegram_attachment_notes(
    paths: AppPaths,
    telegram: TelegramClient,
    message: dict,
    *,
    trace_store: TraceStore | None = None,
    source_event_id: str | None = None,
    chat_id: int | None = None,
    topic_id: int | None = None,
) -> list[str]:
    notes: list[str] = []
    document = message.get("document")
    if isinstance(document, dict) and document.get("file_id"):
        file_name = str(document.get("file_name") or f"document_{document.get('file_unique_id') or document['file_id']}")
        relative_path = save_telegram_file(
            paths,
            telegram,
            file_id=str(document["file_id"]),
            suggested_name=file_name,
            trace_store=trace_store,
            source_event_id=source_event_id,
            chat_id=chat_id,
            topic_id=topic_id,
        )
        mime_type = document.get("mime_type") or mimetypes.guess_type(file_name)[0] or "application/octet-stream"
        notes.append(f"File saved to {relative_path} (name={file_name}, mime={mime_type}).")
    photos = message.get("photo")
    if isinstance(photos, list) and photos:
        photo = photos[-1]
        if isinstance(photo, dict) and photo.get("file_id"):
            photo_name = f"photo_{photo.get('file_unique_id') or photo['file_id']}.jpg"
            relative_path = save_telegram_file(
                paths,
                telegram,
                file_id=str(photo["file_id"]),
                suggested_name=photo_name,
                trace_store=trace_store,
                source_event_id=source_event_id,
                chat_id=chat_id,
                topic_id=topic_id,
            )
            notes.append(f"Image saved to {relative_path}.")
    return notes


def build_telegram_input_text(
    paths: AppPaths,
    telegram: TelegramClient,
    message: dict,
    *,
    trace_store: TraceStore | None = None,
    source_event_id: str | None = None,
    chat_id: int | None = None,
    topic_id: int | None = None,
) -> str:
    base_text = str(message.get("text") or message.get("caption") or "").strip()
    attachment_notes = build_telegram_attachment_notes(
        paths,
        telegram,
        message,
        trace_store=trace_store,
        source_event_id=source_event_id,
        chat_id=chat_id,
        topic_id=topic_id,
    )
    if not attachment_notes:
        return base_text
    parts: list[str] = []
    if base_text:
        parts.append(base_text)
    parts.append("Telegram attachments:")
    parts.extend(f"- {note}" for note in attachment_notes)
    return "\n".join(parts).strip()


def extract_assistant_text(params: dict) -> str | None:
    def _coerce_message_text(value: object) -> str | None:
        if isinstance(value, str) and value.strip():
            return value
        if isinstance(value, list):
            parts = [_coerce_message_text(part) for part in value]
            filtered = [part for part in parts if part]
            if filtered:
                return "\n".join(filtered)
            return None
        if isinstance(value, dict):
            for key in ("text", "delta", "outputText", "finalText", "message", "value"):
                text = _coerce_message_text(value.get(key))
                if text:
                    return text
            for key in ("content", "items", "parts"):
                text = _coerce_message_text(value.get(key))
                if text:
                    return text
        return None

    candidates = [
        params.get("outputText"),
        params.get("delta"),
        params.get("text"),
        params.get("finalText"),
        (params.get("result") or {}).get("outputText") if isinstance(params.get("result"), dict) else None,
        (params.get("result") or {}).get("text") if isinstance(params.get("result"), dict) else None,
    ]
    for candidate in candidates:
        text = _coerce_message_text(candidate)
        if text:
            return text
    turn = params.get("turn")
    if isinstance(turn, dict):
        for item in turn.get("items") or []:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "agentMessage":
                text = _coerce_message_text(item.get("text"))
                if text:
                    return text
    item = params.get("item")
    if isinstance(item, dict) and item.get("type") == "agentMessage":
        phase = item.get("phase")
        if phase == "commentary":
            return None
        text = _coerce_message_text(item.get("text"))
        if text:
            return text
    return None


def _coerce_thinking_value(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, list):
        parts = [part.strip() for part in value if isinstance(part, str) and part.strip()]
        if parts:
            return "\n".join(parts)
    return None


def _shorten_activity_text(text: str, limit: int = 96) -> str:
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3].rstrip() + "..."


def _humanize_status_label(value: str) -> str:
    words = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", value).replace("_", " ").replace("-", " ").split()
    return " ".join(word.capitalize() for word in words)


def _extract_search_hint(arguments: object, *, limit: int = 60) -> str | None:
    if not isinstance(arguments, dict):
        return None
    for key in ("query", "q", "searchTerm", "term", "prompt"):
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            return _shorten_activity_text(value.strip(), limit=limit)
    return None


def _is_site_search_query(query: str) -> bool:
    return query.lstrip().lower().startswith("site:")


def _strip_site_search_prefix(query: str) -> str:
    compact = " ".join(query.split()).strip()
    if not compact:
        return ""
    if _is_site_search_query(compact):
        return compact.split(":", 1)[1].lstrip()
    return compact


def _extract_search_queries(value: object) -> list[str]:
    if not isinstance(value, dict):
        return []
    queries: list[str] = []
    action = value.get("action")
    if isinstance(action, dict):
        action_queries = action.get("queries")
        if isinstance(action_queries, list):
            for candidate in action_queries:
                if isinstance(candidate, str) and candidate.strip():
                    queries.append(" ".join(candidate.split()).strip())
    if not queries:
        query = _extract_search_hint(value, limit=512)
        if query:
            queries.append(query)
        elif isinstance(action, dict):
            query = _extract_search_hint(action, limit=512)
            if query:
                queries.append(query)
    seen: set[str] = set()
    unique: list[str] = []
    for query in queries:
        if query and query not in seen:
            seen.add(query)
            unique.append(query)
    return unique


def _render_search_query(query: str, *, multiline: bool = False) -> str:
    compact = " ".join(query.split()).strip()
    if not compact:
        return ""
    if _is_site_search_query(compact):
        site_query = _strip_site_search_prefix(compact)
        if multiline:
            return f"• {site_query}"
        return f"<pre><code>{escape_telegram_html(site_query)}</code></pre>"
    if multiline:
        return f"• {escape_telegram_html(compact)}"
    return escape_telegram_html(compact)


def _render_search_activity(queries: list[str]) -> str:
    if not queries:
        return ""
    if len(queries) == 1:
        body = _render_search_query(queries[0])
        return f"🌐 Searching:\n{body}" if body else ""
    if all(_is_site_search_query(query) for query in queries):
        body = "\n".join(_render_search_query(query, multiline=True) for query in queries if query.strip())
        rendered = f"<pre><code>{escape_telegram_html(body)}</code></pre>" if body else ""
        return f"🌐 Searching:\n{rendered}" if rendered else ""
    body = "\n".join(_render_search_query(query, multiline=True) for query in queries if query.strip())
    return f"🌐 Searching:\n{body}" if body else ""


def _extract_delta_text(params: dict, *, limit: int = 80) -> str | None:
    for key in ("delta", "text", "summary", "outputText"):
        value = params.get(key)
        if isinstance(value, str) and value.strip():
            return _shorten_activity_text(value.strip(), limit=limit)
    item = params.get("item")
    if isinstance(item, dict):
        for key in ("delta", "text", "summary", "outputText"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return _shorten_activity_text(value.strip(), limit=limit)
    return None


def _extract_file_change_paths(item: object) -> list[str]:
    if not isinstance(item, dict):
        return []
    changes = item.get("changes")
    if not isinstance(changes, list):
        return []
    paths: list[str] = []
    for change in changes:
        if not isinstance(change, dict):
            continue
        path = change.get("path")
        if isinstance(path, str) and path.strip():
            normalized = " ".join(path.split()).strip()
            if normalized and normalized not in paths:
                paths.append(normalized)
    return paths


def _render_file_change_activity(item: object) -> str:
    if not isinstance(item, dict):
        return "Applying file changes"
    paths = _extract_file_change_paths(item)
    status = str(item.get("status") or "").lower()
    if not paths:
        if status == "completed":
            return "Updated files"
        if status in {"inprogress", "in_progress", "started"}:
            return "Updating files"
        return "Applying file changes"
    if len(paths) == 1:
        if status == "completed":
            return f"Updated {paths[0]}"
        return f"Updating {paths[0]}"
    prefix = "Updated files:" if status == "completed" else "Updating files:"
    return f"{prefix}\n" + "\n".join(f"• {path}" for path in paths)


def _extract_command_label(command: str, *, limit: int = 4096) -> str:
    compact = " ".join(command.split())
    shell_wrapper = re.match(r'^(?:/[\w./-]+/)?(?:zsh|bash|sh)\s+-lc\s+["\'](?P<body>.+)["\']$', compact)
    if shell_wrapper:
        compact = shell_wrapper.group("body")
    python_wrapper = re.match(r'^(?:/[\w./-]+/)?python(?:3(?:\.\d+)?)?\s+-c\s+["\'](?P<body>.+)["\']$', compact)
    if python_wrapper:
        compact = python_wrapper.group("body")
    return _shorten_activity_text(compact, limit=limit)


def _encode_command_activity(command: str) -> str:
    return f"{_COMMAND_ACTIVITY_PREFIX}{command}"


def _decode_command_activity(text: str | None) -> str | None:
    if not isinstance(text, str):
        return None
    if not text.startswith(_COMMAND_ACTIVITY_PREFIX):
        return None
    command = text[len(_COMMAND_ACTIVITY_PREFIX) :].strip()
    return command or None


def extract_activity_text(method: str, params: dict) -> str | None:
    if method == "item/commandExecution/outputDelta":
        delta = _extract_delta_text(params)
        if delta:
            return delta
        return "Running command..."
    if method == "item/fileChange/outputDelta":
        delta = _extract_delta_text(params)
        if delta:
            return f"Applying file changes: {delta}"
        return "Applying file changes..."
    if method == "item/plan/delta":
        delta = _extract_delta_text(params)
        if delta:
            return f"Planning: {delta}"
        return "Planning next steps..."
    if method == "serverRequest/resolved":
        return "Approval resolved."
    item = params.get("item")
    if not isinstance(item, dict):
        return None
    item_type = str(item.get("type") or "")
    if item_type == "commandExecution":
        command = item.get("command")
        if isinstance(command, str) and command.strip():
            return _encode_command_activity(_extract_command_label(command.strip()))
        return "Running command"
    if item_type == "mcpToolCall":
        server = str(item.get("server") or "").strip()
        tool = str(item.get("tool") or "").strip()
        arguments = item.get("arguments")
        hint = _extract_search_hint(arguments)
        if isinstance(server, str) and server and isinstance(tool, str) and tool:
            if hint:
                return f"Tool: {server}/{tool} ({hint})"
            return f"Tool: {server}/{tool}"
        if isinstance(tool, str) and tool:
            if hint:
                return f"Tool: {tool} ({hint})"
            return f"Tool: {tool}"
        return "Using external tool"
    if item_type == "dynamicToolCall":
        tool = item.get("tool")
        arguments = item.get("arguments")
        if isinstance(tool, str) and tool:
            lowered = tool.lower()
            hint = _extract_search_hint(arguments, limit=512)
            if "search" in lowered and hint:
                return _render_search_activity([hint])
            if hint:
                return f"Tool: {tool} ({hint})"
            return f"Tool: {tool}"
        return "Using tool"
    if item_type == "collabAgentToolCall":
        tool = str(item.get("tool") or "")
        status = str(item.get("status") or "")
        if tool == "spawnAgent":
            return "Tool: spawning helper agent"
        if tool in {"wait", "closeAgent", "resumeAgent", "sendInput"}:
            return "Tool: coordinating helper agent"
        if status:
            return "Tool: coordinating helper agents"
        return "Tool: helper agent"
    if item_type == "fileChange":
        return _render_file_change_activity(item)
    if item_type == "plan":
        return "Planning next steps"
    if item_type == "search":
        queries = _extract_search_queries(item)
        if queries:
            return _render_search_activity(queries)
        return "🌐"
    if item_type == "webSearch":
        queries = _extract_search_queries(item)
        if queries:
            return _render_search_activity(queries)
        return "🌐"
    return None


def extract_event_driven_status(method: str, params: dict) -> str | None:
    if method == "serverRequest/resolved":
        return None
    if method == "thread/status/changed":
        status_value = params.get("status")
        if isinstance(status_value, dict):
            status_type = status_value.get("type")
            if isinstance(status_type, str) and status_type:
                if status_type == "active":
                    active_flags = status_value.get("activeFlags")
                    if isinstance(active_flags, list) and active_flags:
                        first_flag = active_flags[0]
                        if isinstance(first_flag, str) and first_flag:
                            label = _humanize_status_label(first_flag)
                            if label:
                                return label
                    return None
                if status_type == "systemError":
                    return None
                if status_type == "idle":
                    return None
                label = _humanize_status_label(status_type)
                if label:
                    return label
        status = str(status_value or "").lower()
        if status == "idle":
            return None
        if status in {"running", "in_progress", "working"}:
            return "Working..."
    return None


def maybe_stream_partial_output(
    auth: AuthState,
    telegram: TelegramClient,
    recorder: Recorder,
    session_store: SessionStore,
    session,
    *,
    performance: PerformanceTracker | None = None,
    now: datetime | None = None,
    min_interval_seconds: float = 0.6,
) -> None:
    if not session.pending_output_text.strip():
        session_store.save_session(session)
        return
    if _streaming_message_ids(session):
        effective_now = now or datetime.now(timezone.utc)
        last_sent_at = parse_utc_timestamp(session.last_agent_message_at)
        if (
            last_sent_at is not None
            and min_interval_seconds > 0
            and (effective_now - last_sent_at).total_seconds() < min_interval_seconds
        ):
            session_store.save_session(session)
            return
    flush_buffer(
        session.session_id,
        auth,
        telegram,
        recorder,
        session_store,
        mark_agent=False,
        stream_format=True,
        queue_only=True,
        performance=performance,
    )


def replace_pending_output(session_store: SessionStore, session, text: str) -> None:
    session.pending_output_text = text
    session.pending_output_updated_at = utc_now()
    session_store.save_session(session)


def extract_thinking_text(params: dict) -> str | None:
    candidates = [
        params.get("reasoning"),
        params.get("thinking"),
        params.get("summary"),
        params.get("thought"),
    ]
    for candidate in candidates:
        text = _coerce_thinking_value(candidate)
        if text:
            return text
    item = params.get("item")
    if isinstance(item, dict):
        item_type = str(item.get("type") or "").lower()
        if item_type in {"reasoning", "thinking", "thought", "reasoningsummary"}:
            for key in ("text", "summary", "content"):
                text = _coerce_thinking_value(item.get(key))
                if text:
                    return text
    items = params.get("items")
    if isinstance(items, list):
        for candidate in reversed(items):
            if not isinstance(candidate, dict):
                continue
            item_type = str(candidate.get("type") or "").lower()
            if item_type not in {"reasoning", "thinking", "thought", "reasoningsummary"}:
                continue
            for key in ("text", "summary", "content"):
                text = _coerce_thinking_value(candidate.get(key))
                if text:
                    return text
    return None


def extract_thinking_delta(method: str, params: dict) -> str | None:
    if method in {
        "item/reasoning/textDelta",
        "item/reasoning/summaryTextDelta",
        "agent_reasoning_delta",
        "agent_reasoning_raw_content_delta",
        "reasoning_content_delta",
        "reasoning_raw_content_delta",
    }:
        delta = params.get("delta")
        if isinstance(delta, str) and delta:
            return delta
    if method in {"agent_reasoning", "agent_reasoning_raw_content"}:
        text = params.get("text")
        if isinstance(text, str) and text.strip():
            return text.strip()
    if method in {"item/reasoning/summaryPartAdded", "agent_reasoning_section_break"}:
        return "\n"
    return None


def default_thinking_text(session) -> str:
    if not session.last_user_message_at:
        return "Thinking"
    started_at = parse_utc_timestamp(session.last_user_message_at)
    if started_at is None:
        return "Thinking"
    elapsed = max((datetime.now(timezone.utc) - started_at).total_seconds(), 0.0)
    dots = int(elapsed) % 4
    return f"Thinking{'.' * dots}"


def is_default_thinking_text(text: str | None) -> bool:
    if not text:
        return True
    return text in {"Thinking", "Thinking.", "Thinking..", "Thinking..."}


def ensure_thinking_message(
    auth: AuthState,
    telegram: TelegramClient,
    session,
    *,
    text: str | None = None,
    performance: PerformanceTracker | None = None,
) -> None:
    display_text = text or default_thinking_text(session)
    session.thinking_message_text = display_text


def render_thinking_message(text: str | None) -> str:
    return render_telegram_progress_html(text)


def extract_thinking_body(text: str | None) -> str:
    value = (text or "").strip()
    if value.startswith("Thinking\n\n"):
        return value[len("Thinking\n\n") :].strip()
    if value == "Thinking":
        return ""
    return value


def _thinking_history_entries(session) -> list[str]:
    ordered: list[str] = []
    for source_key in session.thinking_history_order:
        text = session.thinking_history_by_source.get(source_key, "").strip()
        if text:
            ordered.append(text)
    if ordered:
        return ordered
    return [line for line in session.thinking_history_text.split("\n") if line.strip()]


def _current_live_thinking_entries(session) -> list[str]:
    ordered: list[str] = []
    for source_key in session.thinking_history_order:
        text = session.thinking_live_texts.get(source_key, "").strip()
        if text:
            ordered.append(text)
    for source_key, text in session.thinking_live_texts.items():
        normalized = text.strip()
        if normalized and source_key not in session.thinking_history_order:
            ordered.append(normalized)
    return ordered


def _current_live_thinking_source_entries(session) -> list[tuple[str, str]]:
    ordered: list[tuple[str, str]] = []
    seen: set[str] = set()
    for source_key in session.thinking_history_order:
        text = session.thinking_live_texts.get(source_key, "").strip()
        if text:
            ordered.append((source_key, text))
            seen.add(source_key)
    for source_key, text in session.thinking_live_texts.items():
        normalized = text.strip()
        if normalized and source_key not in seen:
            ordered.append((source_key, normalized))
    return ordered


def _render_live_thinking_html(session) -> str:
    entries = _current_live_thinking_entries(session)
    if not entries:
        return ""
    rendered_entries = [render_telegram_progress_html(entry) for entry in entries]
    rendered_entries = [entry.strip() for entry in rendered_entries if entry.strip()]
    return "\n\n".join(rendered_entries)


def _record_thinking_history(session, source_key: str, text: str) -> None:
    normalized = text.strip()
    if not normalized:
        return
    if source_key not in session.thinking_history_order:
        session.thinking_history_order.append(source_key)
    session.thinking_history_by_source[source_key] = normalized
    session.thinking_history_text = "\n".join(_thinking_history_entries(session))


def _archive_visible_thinking_segment(session) -> None:
    live_entries = _current_live_thinking_source_entries(session)
    if not live_entries and not _streaming_message_ids(session):
        return
    segment_token = utc_now()
    for source_key, text in live_entries:
        if source_key in session.thinking_history_by_source:
            session.thinking_history_by_source.pop(source_key, None)
        session.thinking_history_order = [entry for entry in session.thinking_history_order if entry != source_key]
        archive_key = f"segment:{segment_token}:{len(session.thinking_history_order)}"
        session.thinking_history_order.append(archive_key)
        session.thinking_history_by_source[archive_key] = text
    session.thinking_history_text = "\n".join(_thinking_history_entries(session))
    archived_ids = [message_id for message_id in session.thinking_message_ids if isinstance(message_id, int)]
    for message_id in _streaming_message_ids(session):
        if message_id not in archived_ids:
            archived_ids.append(message_id)
    session.thinking_message_ids = archived_ids
    _set_streaming_message_ids(session, [])
    session.thinking_message_text = ""
    session.last_thinking_sent_text = ""
    session.thinking_live_texts = {}
    session.thinking_sent_texts = {}
    session.thinking_live_message_ids = {}


def _capture_thinking_segment_snapshot(session) -> dict[str, object]:
    return {
        "streaming_message_id": session.streaming_message_id,
        "streaming_message_ids": list(session.streaming_message_ids),
        "thinking_message_id": session.thinking_message_id,
        "thinking_message_ids": list(session.thinking_message_ids),
        "thinking_live_message_ids": dict(session.thinking_live_message_ids),
        "thinking_live_texts": dict(session.thinking_live_texts),
        "thinking_sent_texts": dict(session.thinking_sent_texts),
        "thinking_message_text": session.thinking_message_text,
        "thinking_history_text": session.thinking_history_text,
        "thinking_history_order": list(session.thinking_history_order),
        "thinking_history_by_source": dict(session.thinking_history_by_source),
        "last_thinking_sent_text": session.last_thinking_sent_text,
    }


def _restore_thinking_segment_snapshot(session, snapshot: dict[str, object]) -> None:
    session.streaming_message_id = snapshot["streaming_message_id"]
    session.streaming_message_ids = list(snapshot["streaming_message_ids"])
    session.thinking_message_id = snapshot["thinking_message_id"]
    session.thinking_message_ids = list(snapshot["thinking_message_ids"])
    session.thinking_live_message_ids = dict(snapshot["thinking_live_message_ids"])
    session.thinking_live_texts = dict(snapshot["thinking_live_texts"])
    session.thinking_sent_texts = dict(snapshot["thinking_sent_texts"])
    session.thinking_message_text = str(snapshot["thinking_message_text"])
    session.thinking_history_text = str(snapshot["thinking_history_text"])
    session.thinking_history_order = list(snapshot["thinking_history_order"])
    session.thinking_history_by_source = dict(snapshot["thinking_history_by_source"])
    session.last_thinking_sent_text = str(snapshot["last_thinking_sent_text"])


def derive_thinking_source_key(
    method: str,
    params: dict,
    *,
    agent_message_phase: str | None = None,
    activity_text: str | None = None,
    status_text: str | None = None,
    thinking_text: str | None = None,
) -> str | None:
    item = params.get("item") if isinstance(params.get("item"), dict) else {}
    item_type = str(item.get("type") or "")
    item_id = params.get("itemId") or item.get("id")
    turn_id = params.get("turnId") or params.get("turn_id") or ""
    if agent_message_phase == "commentary":
        identifier = item_id or turn_id or "current"
        return f"commentary:{identifier}"
    if method.startswith("item/reasoning") or item_type.lower() in {"reasoning", "thinking", "thought", "reasoningsummary"}:
        identifier = item_id or turn_id or "current"
        return f"reasoning:{identifier}"
    if activity_text:
        if method == "item/commandExecution/outputDelta":
            identifier = item_id or turn_id or "current"
            return f"command-output:{identifier}"
        if item_type == "commandExecution":
            identifier = item_id or turn_id or "current"
            return f"command:{identifier}"
        if item_type:
            identifier = item_id or turn_id or "current"
            return f"activity:{item_type}:{identifier}"
        return f"activity:{method}:{turn_id or 'current'}"
    if thinking_text:
        identifier = item_id or turn_id or "current"
        return f"thinking:{identifier}"
    if status_text:
        return f"status:{status_text.lower()}"
    return None


def _is_meaningful_live_thinking_text(text: str | None) -> bool:
    body = (text or "").strip()
    if not body:
        return False
    if _decode_command_activity(body):
        return True
    if body in {"Thinking", "Running"}:
        return False
    if len(body) >= MIN_LIVE_THINKING_LENGTH:
        return True
    if any(char in body for char in {" ", "\n", ".", "!", "?", ":"}):
        return True
    return False


def _drop_placeholder_thinking_state(session) -> None:
    if THINKING_PLACEHOLDER_SOURCE_KEY in session.thinking_live_texts:
        session.thinking_live_texts.pop(THINKING_PLACEHOLDER_SOURCE_KEY, None)
    if THINKING_PLACEHOLDER_SOURCE_KEY in session.thinking_sent_texts:
        session.thinking_sent_texts.pop(THINKING_PLACEHOLDER_SOURCE_KEY, None)
    if THINKING_PLACEHOLDER_SOURCE_KEY in session.thinking_history_by_source:
        session.thinking_history_by_source.pop(THINKING_PLACEHOLDER_SOURCE_KEY, None)
    session.thinking_history_order = [
        entry for entry in session.thinking_history_order if entry != THINKING_PLACEHOLDER_SOURCE_KEY
    ]
    session.thinking_history_text = "\n".join(_thinking_history_entries(session))


def _should_render_thinking_placeholder(session) -> bool:
    if not session.last_user_message_at:
        return False
    started_at = parse_utc_timestamp(session.last_user_message_at)
    if started_at is None:
        return False
    elapsed = max((datetime.now(timezone.utc) - started_at).total_seconds(), 0.0)
    return elapsed >= DEFAULT_THINKING_PLACEHOLDER_DELAY_SECONDS


def set_visible_thinking_message(
    auth: AuthState,
    telegram: TelegramClient,
    recorder: Recorder,
    session_store: SessionStore,
    session,
    *,
    text: str | None = None,
    source_key: str | None = None,
    performance: PerformanceTracker | None = None,
    min_interval_seconds: float = DEFAULT_THINKING_STREAM_MIN_INTERVAL_SECONDS,
    allow_placeholder: bool = False,
) -> None:
    ensure_thinking_message(auth, telegram, session, text=text, performance=performance)
    target_chat_id = session.transport_chat_id or auth.telegram_chat_id
    if not session.attached or not target_chat_id:
        session_store.save_session(session)
        return
    effective_source = source_key or f"misc:{uuid.uuid4()}"
    if effective_source != THINKING_PLACEHOLDER_SOURCE_KEY:
        _drop_placeholder_thinking_state(session)
    current_text = session.thinking_message_text.strip()
    if not allow_placeholder and not _is_meaningful_live_thinking_text(current_text):
        session_store.save_session(session)
        return
    _record_thinking_history(session, effective_source, current_text)
    previous_text = session.thinking_sent_texts.get(effective_source, "").strip()
    if previous_text == current_text:
        session_store.save_session(session)
        return
    throttle_key = f"{session.session_id}:{effective_source}"
    now_monotonic = time.monotonic()
    last_sent_at = _THINKING_SOURCE_LAST_SENT_AT.get(throttle_key)
    if last_sent_at is None and effective_source.startswith("commentary:") and session.last_agent_message_at:
        recorded_last_sent_at = parse_utc_timestamp(session.last_agent_message_at)
        if recorded_last_sent_at is not None:
            age_seconds = max((datetime.now(timezone.utc) - recorded_last_sent_at).total_seconds(), 0.0)
            last_sent_at = now_monotonic - age_seconds
    if last_sent_at is not None and (now_monotonic - last_sent_at) < min_interval_seconds:
        session.thinking_live_texts[effective_source] = current_text
        session_store.save_session(session)
        return
    session.thinking_live_texts[effective_source] = current_text
    rendered = _render_live_thinking_html(session)
    if not rendered.strip():
        session_store.save_session(session)
        return
    context = {
        "category": "thinking_output",
        "session_id": session.session_id,
        "thread_id": session.thread_id,
        "turn_id": session.active_turn_id or session.last_completed_turn_id,
    }
    _THINKING_SOURCE_LAST_SENT_AT[throttle_key] = now_monotonic
    try:
        _sync_telegram_message_chunks(
            session_store.paths,
            telegram,
            target_chat_id,
            session=session,
            rendered_chunks=_split_telegram_html_text(rendered),
            topic_id=session.transport_topic_id,
            parse_mode=TELEGRAM_PARSE_MODE,
            disable_notification=True,
            queue_only=True,
            performance=performance,
            context=context,
        )
    except Exception:
        session_store.save_session(session)
        return
    session.thinking_sent_texts[effective_source] = current_text
    session.last_thinking_sent_text = current_text
    session.thinking_message_text = rendered
    if effective_source.startswith("commentary:"):
        session.streaming_phase = "commentary"
    session_store.mark_agent_message(session)
    session_store.save_session(session)


def clear_thinking_message(
    auth: AuthState,
    telegram: TelegramClient,
    session_store: SessionStore,
    session,
    *,
    performance: PerformanceTracker | None = None,
) -> None:
    prefix = f"{session.session_id}:"
    for key in [key for key in _THINKING_SOURCE_LAST_SENT_AT if key.startswith(prefix)]:
        _THINKING_SOURCE_LAST_SENT_AT.pop(key, None)
    target_chat_id = session.transport_chat_id or auth.telegram_chat_id
    if target_chat_id:
        archived_ids = [message_id for message_id in session.thinking_message_ids if isinstance(message_id, int)]
        for message_id in archived_ids:
            try:
                delete_telegram_message(
                    telegram,
                    target_chat_id,
                    message_id,
                    allow_paused_return=True,
                    performance=performance,
                )
            except Exception:
                pass
    session.thinking_message_id = None
    session.thinking_message_ids = []
    session.thinking_live_message_ids = {}
    session.thinking_live_texts = {}
    session.thinking_sent_texts = {}
    session.thinking_message_text = ""
    session.last_thinking_sent_text = ""
    session_store.save_session(session)


def maybe_refresh_thinking_message(
    paths: AppPaths,
    auth: AuthState,
    telegram: TelegramClient,
    session_store: SessionStore,
    *,
    recorder: Recorder | None = None,
    performance: PerformanceTracker | None = None,
) -> None:
    if ApprovalStore(paths).pending() or recorder is None:
        return
    for session in session_store.list_telegram_sessions(auth):
        if (
            not session.attached
            or not session.active_turn_id
            or session.status != "RUNNING_TURN"
            or not session.last_user_message_at
        ):
            continue
        if is_stale_active_turn(session):
            continue
        non_placeholder_live_entries = {
            key: value
            for key, value in session.thinking_live_texts.items()
            if key != THINKING_PLACEHOLDER_SOURCE_KEY
        }
        if not non_placeholder_live_entries:
            if not _should_render_thinking_placeholder(session):
                continue
            set_visible_thinking_message(
                auth,
                telegram,
                recorder,
                session_store,
                session,
                text=default_thinking_text(session),
                source_key=THINKING_PLACEHOLDER_SOURCE_KEY,
                performance=performance,
                allow_placeholder=True,
            )
            continue
        _drop_placeholder_thinking_state(session)
        for source_key, source_text in list(session.thinking_live_texts.items()):
            if source_key == THINKING_PLACEHOLDER_SOURCE_KEY:
                continue
            if not source_text.strip():
                continue
            set_visible_thinking_message(
                auth,
                telegram,
                recorder,
                session_store,
                session,
                text=source_text,
                source_key=source_key,
                performance=performance,
                min_interval_seconds=0.0,
            )


def append_thinking_delta(
    auth: AuthState,
    telegram: TelegramClient,
    session,
    delta: str,
    *,
    source_key: str | None = None,
    performance: PerformanceTracker | None = None,
) -> None:
    if not delta:
        return
    effective_source = source_key or "thinking:current"
    existing_text = session.thinking_live_texts.get(effective_source, "") or session.thinking_message_text or extract_thinking_body(session.streaming_output_text)
    if existing_text:
        separator = ""
        if (
            not existing_text.endswith((" ", "\n"))
            and not delta.startswith((" ", "\n", ".", ",", ";", ":", "!", "?", ")"))
        ):
            separator = " "
        next_text = f"{existing_text}{separator}{delta}"
    else:
        next_text = delta
    session.thinking_live_texts[effective_source] = next_text.strip() or next_text
    ensure_thinking_message(auth, telegram, session, text=next_text.strip() or next_text, performance=performance)


def extract_turn_id(params: dict) -> str | None:
    turn_id = params.get("turnId")
    if isinstance(turn_id, str) and turn_id:
        return turn_id
    turn = params.get("turn")
    if isinstance(turn, dict):
        nested = turn.get("id")
        if isinstance(nested, str) and nested:
            return nested
    return None


def extract_latest_agent_message(thread_payload: dict) -> str | None:
    thread = thread_payload.get("thread")
    if not isinstance(thread, dict):
        return None
    turns = thread.get("turns")
    if not isinstance(turns, list):
        return None
    for turn in reversed(turns):
        if not isinstance(turn, dict):
            continue
        items = turn.get("items")
        if not isinstance(items, list):
            continue
        for item in reversed(items):
            if not isinstance(item, dict):
                continue
            if item.get("type") == "agentMessage":
                text = item.get("text")
                if isinstance(text, str) and text.strip():
                    return text
    return None


def extract_account_payload(params: dict) -> dict | None:
    if not isinstance(params, dict):
        return None
    for key in ("account", "result"):
        candidate = params.get(key)
        if isinstance(candidate, dict):
            return candidate
    if any(key in params for key in ("status", "state", "success", "authMode", "planType", "accountType", "type")):
        return params
    return None


def extract_login_callback_url(text: str) -> str | None:
    match = LOCAL_AUTH_CALLBACK_RE.search(text)
    if not match:
        return None
    candidate = match.group(0).rstrip(").,]")
    parsed = urlparse(candidate)
    params = parse_qs(parsed.query)
    if not params.get("code") or not params.get("state"):
        return None
    return candidate


def replay_login_callback(callback_url: str, timeout_seconds: float = 5.0) -> tuple[bool, str]:
    try:
        with urlopen(callback_url, timeout=timeout_seconds) as response:
            body = response.read(512).decode("utf-8", errors="replace").strip()
            if response.status >= 400:
                return False, f"HTTP {response.status}"
            return True, body or "Codex login callback accepted."
    except HTTPError as exc:
        return False, f"HTTP {exc.code}"
    except URLError as exc:
        reason = getattr(exc, "reason", exc)
        return False, str(reason)


def update_codex_auth_state(
    paths: AppPaths,
    *,
    account_payload: dict,
    runtime: ServiceRuntime | None,
    runtime_state: RuntimeState | None,
) -> str:
    persisted = load_codex_server_state(paths)
    if persisted is None:
        persisted = CodexServerState(transport="stdio://", initialized=True)
    account_info = account_payload.get("account") if isinstance(account_payload.get("account"), dict) else {}
    next_state = derive_codex_state(account_payload)
    if account_payload.get("success") is True and next_state == "AUTH_REQUIRED":
        next_state = "RUNNING"
    persisted.account_status = (
        account_payload.get("status")
        or account_payload.get("state")
        or ("ready" if next_state == "RUNNING" else None)
    )
    persisted.account_type = (
        account_payload.get("accountType")
        or account_payload.get("type")
        or account_payload.get("authMode")
        or account_info.get("accountType")
        or account_info.get("type")
    )
    persisted.auth_required = next_state == "AUTH_REQUIRED"
    if not persisted.auth_required:
        persisted.login_url = None
        persisted.login_type = None
    save_codex_server_state(paths, persisted)
    if runtime is not None and runtime_state is not None:
        runtime.set_codex_state(next_state)
        save_runtime_state(paths, runtime_state)
    return next_state


def _read_thread_completion_text(codex, thread_id: str | None) -> str | None:
    if not thread_id or codex is None or not hasattr(codex, "read_thread"):
        return None
    try:
        payload = codex.read_thread(thread_id, include_turns=True)
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    return extract_latest_agent_message(payload)


def _session_accepts_turn_notification(session, turn_id: str | None) -> bool:
    if not turn_id:
        return True
    if session.active_turn_id == turn_id:
        return True
    if session.last_completed_turn_id == turn_id:
        return False
    return True


def _session_accepts_live_output_notification(session, turn_id: str | None) -> bool:
    active_turn_id = getattr(session, "active_turn_id", None)
    if not isinstance(active_turn_id, str) or not active_turn_id:
        completed_turn_id = getattr(session, "last_completed_turn_id", None)
        return not (isinstance(completed_turn_id, str) and completed_turn_id)
    if not turn_id:
        return True
    return active_turn_id == turn_id


def resolve_notification_session(
    session_store: SessionStore,
    auth: AuthState,
    params: dict,
):
    turn_id = params.get("turnId")
    normalized_turn_id = str(turn_id) if turn_id else None
    thread_id = params.get("threadId")
    if thread_id:
        session = session_store.find_by_thread_id(str(thread_id))
        if (
            session is not None
            and session_store.is_recoverable(session)
            and _session_accepts_turn_notification(session, normalized_turn_id)
        ):
            return session
        return None
    if normalized_turn_id:
        session = session_store.find_by_turn_id(normalized_turn_id)
        if (
            session is not None
            and session_store.is_recoverable(session)
            and _session_accepts_turn_notification(session, normalized_turn_id)
        ):
            return session
        completed = session_store.find_by_completed_turn_id(normalized_turn_id)
        if completed is not None:
            return None
    active = session_store.get_active_telegram_session(auth)
    if active is None:
        return None
    if active.thread_id is not None:
        return None
    return active


def flush_buffer(
    session_id: str,
    auth: AuthState,
    telegram: TelegramClient,
    recorder: Recorder,
    session_store: SessionStore,
    *,
    mark_agent: bool,
    stream_format: bool = False,
    queue_only: bool = False,
    performance: PerformanceTracker | None = None,
) -> None:
    session = next((item for item in session_store.load().sessions if item.session_id == session_id), None)
    if session is None:
        return
    target_chat_id = session.transport_chat_id or auth.telegram_chat_id
    pending_text = session.pending_output_text
    if not pending_text.strip():
        return
    text = pending_text.strip()
    if session.streaming_phase == "commentary":
        text = pending_text.strip()
    elif session.streaming_output_text:
        streamed_text = session.streaming_output_text.strip()
        if text.startswith(streamed_text):
            text = text
        else:
            text = f"{session.streaming_output_text}{pending_text}".strip()
    if not session.attached or not target_chat_id:
        append_recovery_event(
            session_store.paths,
            f"hidden_session_output_consumed {session_log_label(session)} delivered_to_telegram=false",
            trace_id=getattr(session, "current_trace_id", None),
            session_id=session.session_id,
            thread_id=session.thread_id,
            turn_id=session.active_turn_id or session.last_completed_turn_id,
            chat_id=target_chat_id,
            topic_id=session.transport_topic_id,
        )
        if mark_agent:
            session_store.mark_agent_message(session)
        session_store.consume_pending_output(session)
        pruned = session_store.prune_detached_sessions()
        if pruned:
            append_recovery_event(session_store.paths, f"detached_sessions_pruned count={pruned}")
        return
    if text == session.last_delivered_output_text:
        should_finalize_existing_delivery = (
            mark_agent
            and bool(_streaming_message_ids(session))
            and (bool(_thinking_history_entries(session)) or delivery_manager_supports_background_queue())
        )
        if mark_agent and not should_finalize_existing_delivery:
            session.streaming_message_id = None
            session.streaming_message_ids = []
            session.thinking_message_id = None
            session.thinking_message_ids = []
            session.thinking_live_message_ids = {}
            session.thinking_live_texts = {}
            session.thinking_sent_texts = {}
            session.streaming_output_text = ""
            session.streaming_phase = ""
            session.thinking_message_text = ""
            session.thinking_history_text = ""
            session.thinking_history_order = []
            session.thinking_history_by_source = {}
            session.last_thinking_sent_text = ""
            session_store.save_session(session)
        if not should_finalize_existing_delivery:
            session_store.consume_pending_output(session)
            return
    if not text.strip():
        return
    context = {
        "performance": performance,
        "category": "assistant_output",
        "session_id": session.session_id,
        "thread_id": session.thread_id,
        "turn_id": session.active_turn_id or session.last_completed_turn_id,
    }
    if not mark_agent:
        normalized_text = normalize_legacy_telegram_text(text)
        try:
            if stream_format:
                answer_html = (
                    repair_partial_telegram_html(normalized_text.strip())
                    if looks_like_telegram_html(normalized_text)
                    else to_telegram_html(normalized_text)
                )
                thinking_html = render_collapsed_thinking_html(_thinking_history_entries(session))
                rendered_chunks = _build_final_rendered_chunks(answer_html=answer_html, thinking_html=thinking_html)
                parse_mode = TELEGRAM_PARSE_MODE
            else:
                rendered_chunks = split_telegram_text(text)
                parse_mode = None
            _sync_telegram_message_chunks(
                session_store.paths,
                telegram,
                target_chat_id,
                session=session,
                rendered_chunks=rendered_chunks,
                topic_id=session.transport_topic_id,
                parse_mode=parse_mode,
                disable_notification=False,
                queue_only=queue_only,
                performance=performance,
                context=context,
            )
        except Exception:
            session_store.save_session(session)
            return
        session.streaming_output_text = text
        session.streaming_phase = "answer"
        session.thinking_message_text = ""
        session.last_thinking_sent_text = ""
        session.thinking_live_texts = {}
        session.thinking_sent_texts = {}
        session_store.mark_delivered_output(session, text)
        session_store.mark_agent_message(session)
        session_store.consume_pending_output(session)
        recorder.record("assistant", text)
        return

    normalized_text = normalize_legacy_telegram_text(text)
    answer_html = (
        repair_partial_telegram_html(normalized_text.strip())
        if looks_like_telegram_html(normalized_text)
        else to_telegram_html(normalized_text)
    )
    thinking_html = render_collapsed_thinking_html(_thinking_history_entries(session))
    final_html = answer_html if not thinking_html else f"{thinking_html}\n\n{answer_html}"
    _cancel_queued_live_progress_operations(session_store.paths, session, include_typing=True)
    rendered_attempts = [
        ("formatted_html", _build_final_rendered_chunks(answer_html=answer_html, thinking_html=thinking_html)),
        (
            "escaped_html",
            _build_final_rendered_chunks(
                answer_html=escape_telegram_html(normalized_text),
                thinking_html=thinking_html,
            ),
        ),
    ]
    delivered = False
    for stage, rendered_chunks in rendered_attempts:
        try:
            _sync_telegram_message_chunks(
                session_store.paths,
                telegram,
                target_chat_id,
                session=session,
                rendered_chunks=rendered_chunks,
                topic_id=session.transport_topic_id,
                parse_mode=TELEGRAM_PARSE_MODE,
                disable_notification=False,
                queue_only=queue_only,
                performance=performance,
                context=context,
            )
            delivered = True
            break
        except Exception as exc:
            append_telegram_format_failure_log(
                session_store.paths,
                session_id=session.session_id,
                trace_id=getattr(session, "current_trace_id", None),
                thread_id=session.thread_id,
                turn_id=session.active_turn_id or session.last_completed_turn_id,
                stage=stage,
                error=str(exc),
                raw_text=text,
                rich_text=final_html,
                escaped_text="\n\n".join(rendered_attempts[-1][1]),
                emergency_text=None,
            )
    if not delivered:
        raise TelegramError("All Telegram HTML delivery attempts failed.")
    if queue_only and delivery_manager_supports_background_queue():
        session.streaming_output_text = text
        session.streaming_phase = "finalizing"
        session.status = "DELIVERING_FINAL"
        session_store.save_session(session)
        return
    clear_thinking_message(auth, telegram, session_store, session, performance=performance)
    recorder.record("assistant", text)
    session.streaming_message_id = None
    session.streaming_message_ids = []
    session.streaming_output_text = ""
    session.streaming_phase = ""
    session.last_thinking_sent_text = ""
    session_store.mark_delivered_output(session, text)
    session_store.mark_agent_message(session)
    session.thinking_history_text = ""
    session.thinking_history_order = []
    session.thinking_history_by_source = {}
    session.thinking_message_text = ""
    session_store.save_session(session)
    session_store.consume_pending_output(session)
    pruned = session_store.prune_detached_sessions()
    if pruned:
        append_recovery_event(session_store.paths, f"detached_sessions_pruned count={pruned}")


def should_append_completion_text(session, assistant_text: str | None) -> bool:
    if not assistant_text or not assistant_text.strip():
        return False
    candidate = assistant_text.strip()
    if session.pending_output_text.strip() == candidate:
        return False
    if session.streaming_output_text.strip() == candidate:
        return False
    if not session.pending_output_text.strip() and session.last_delivered_output_text.strip() == candidate:
        return False
    return True


def _common_prefix_length(left: str, right: str) -> int:
    limit = min(len(left), len(right))
    index = 0
    while index < limit and left[index] == right[index]:
        index += 1
    return index


def _suffix_prefix_overlap(left: str, right: str) -> int:
    limit = min(len(left), len(right))
    for size in range(limit, 0, -1):
        if left[-size:] == right[:size]:
            return size
    return 0


def _common_suffix_length(left: str, right: str) -> int:
    limit = min(len(left), len(right))
    index = 0
    while index < limit and left[-(index + 1)] == right[-(index + 1)]:
        index += 1
    return index


def merge_streamed_agent_delta_text(session, text: str | None) -> tuple[str, str]:
    if not text:
        return ("ignore", "")
    incoming = text
    existing_pending = session.pending_output_text
    existing_stream = session.streaming_output_text
    combined = f"{existing_stream}{existing_pending}"
    delivered = session.last_delivered_output_text
    if incoming == existing_pending or incoming == existing_stream or incoming == combined or incoming == delivered:
        return ("ignore", "")
    if combined and incoming.startswith(combined):
        return ("append", incoming[len(combined) :])
    if existing_pending and incoming.startswith(existing_pending):
        return ("append", incoming[len(existing_pending) :])
    if existing_stream and incoming.startswith(existing_stream):
        return ("append", incoming[len(existing_stream) :])
    if delivered and incoming.startswith(delivered):
        return ("append", incoming[len(delivered) :])
    if combined and combined.startswith(incoming):
        return ("ignore", "")
    return ("append", incoming)


def merge_incremental_assistant_text(session, text: str | None) -> tuple[str, str]:
    if not text:
        return ("ignore", "")
    incoming = text
    existing_pending = session.pending_output_text
    existing_stream = session.streaming_output_text
    combined = f"{existing_stream}{existing_pending}"
    delivered = session.last_delivered_output_text
    if incoming == delivered or incoming == combined or incoming == existing_pending or incoming == existing_stream:
        return ("ignore", "")
    if combined and incoming.startswith(combined):
        return ("append", incoming[len(combined) :])
    if existing_pending and incoming.startswith(existing_pending):
        return ("append", incoming[len(existing_pending) :])
    if existing_stream and incoming.startswith(existing_stream):
        return ("append", incoming[len(existing_stream) :])
    if delivered and incoming.startswith(delivered):
        return ("append", incoming[len(delivered) :])
    if combined and combined.startswith(incoming):
        return ("ignore", "")
    prefix = _common_prefix_length(combined, incoming) if combined else 0
    suffix = _common_suffix_length(combined, incoming) if combined else 0
    if combined and (prefix + suffix) >= max(48, int(min(len(combined), len(incoming)) * 0.8)):
        return ("replace", incoming)
    if delivered:
        delivered_prefix = _common_prefix_length(delivered, incoming)
        delivered_suffix = _common_suffix_length(delivered, incoming)
        if (delivered_prefix + delivered_suffix) >= max(48, int(min(len(delivered), len(incoming)) * 0.8)):
            return ("replace", incoming)
    overlap = _suffix_prefix_overlap(combined, incoming) if combined else 0
    if overlap >= max(16, min(len(combined), len(incoming)) // 3):
        return ("append", incoming[overlap:])
    return ("append", incoming)


def parse_utc_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def flush_idle_partial_outputs(
    paths: AppPaths,
    auth: AuthState,
    telegram: TelegramClient,
    recorder: Recorder,
    session_store: SessionStore,
    *,
    idle_seconds: float,
    now: datetime | None = None,
    performance: PerformanceTracker | None = None,
) -> None:
    if idle_seconds <= 0:
        return
    if ApprovalStore(paths).pending():
        return
    now = now or datetime.now(timezone.utc)
    for session in session_store.list_telegram_sessions(auth):
        if not session.attached or not session.pending_output_text:
            continue
        updated_at = parse_utc_timestamp(session.pending_output_updated_at)
        if updated_at is None:
            continue
        if (now - updated_at).total_seconds() < idle_seconds:
            continue
        flush_buffer(
            session.session_id,
            auth,
            telegram,
            recorder,
            session_store,
            mark_agent=False,
            stream_format=True,
            queue_only=True,
            performance=performance,
        )


def maybe_send_typing_indicator(
    paths: AppPaths,
    auth: AuthState,
    telegram: TelegramClient,
    session_store: SessionStore,
    *,
    interval_seconds: float,
    last_sent_at: datetime | None,
    now: datetime | None = None,
    performance: PerformanceTracker | None = None,
) -> datetime | None:
    if interval_seconds <= 0:
        return last_sent_at
    if ApprovalStore(paths).pending():
        return last_sent_at
    current = get_latest_user_session(session_store, auth, require_active_turn=True)
    if current is None or not current.attached or not current.active_turn_id:
        return last_sent_at
    if is_stale_active_turn(current):
        return last_sent_at
    target_chat_id = current.transport_chat_id or auth.telegram_chat_id
    if not target_chat_id:
        return last_sent_at
    now = now or datetime.now(timezone.utc)
    effective_interval = min(interval_seconds, 1.5)
    if last_sent_at is not None and (now - last_sent_at).total_seconds() < effective_interval:
        return last_sent_at
    if hasattr(telegram, "send_typing"):
        typing_group_id = f"{current.session_id}:typing"
        queue_telegram_typing(
            target_chat_id,
            topic_id=current.transport_topic_id,
            performance=performance,
            session_id=current.session_id,
            trace_id=getattr(current, "current_trace_id", None),
            message_group_id=typing_group_id,
            dedupe_key=typing_group_id,
            priority=10,
        )
        return now
    return last_sent_at


def codex_is_alive(codex) -> bool:
    if codex is None:
        return False
    if hasattr(codex, "is_alive"):
        try:
            return bool(codex.is_alive())
        except Exception:
            return False
    return True


def codex_restart_delay(config: Config, failure_count: int) -> float:
    base = max(config.codex_restart_backoff_seconds, 0.0)
    maximum = max(config.codex_restart_backoff_max_seconds, base)
    if base == 0:
        return 0.0
    exponent = max(failure_count - 1, 0)
    return min(base * (2**exponent), maximum)


def telegram_retry_delay(config: Config, failure_count: int) -> float:
    base = max(config.telegram_backoff_seconds, 0.0)
    maximum = max(config.telegram_backoff_max_seconds, base)
    if base == 0:
        return 0.0
    exponent = max(failure_count - 1, 0)
    return min(base * (2**exponent), maximum)


def telegram_unexpected_retry_delay(config: Config, failure_count: int) -> float:
    if failure_count <= 1:
        return 0.0
    return telegram_retry_delay(config, failure_count - 1)


def maintain_codex_runtime(
    *,
    paths: AppPaths,
    config: Config,
    auth: AuthState,
    runtime: ServiceRuntime,
    runtime_state: RuntimeState,
    metadata,
    app_lock,
    telegram: TelegramClient,
    handle_output,
    codex,
    start_codex_session_fn,
    restart_failures: int,
    next_restart_at: float,
    performance: PerformanceTracker | None = None,
) -> tuple[object | None, int, float]:
    now = time.monotonic()
    if codex is not None and not codex_is_alive(codex):
        try:
            codex.stop()
        except Exception:
            pass
        codex = None
        restart_failures += 1
        delay = codex_restart_delay(config, restart_failures)
        next_restart_at = now + delay
        runtime.set_codex_state("BACKOFF")
        save_runtime_state(paths, runtime_state)
        append_recovery_event(
            paths,
            f"codex child exited -> restart backoff={delay:.1f}s",
            run_id=runtime_state.session_id,
        )
    if codex is None and is_auth_paired(auth) and now >= next_restart_at:
        restarted = invoke_start_codex_session_fn(
            start_codex_session_fn,
            config,
            auth,
            runtime,
            runtime_state,
            metadata,
            app_lock,
            telegram,
            handle_output,
            performance,
        )
        if restarted is not None:
            append_recovery_event(paths, "codex restart succeeded", run_id=runtime_state.session_id)
            TraceStore(paths, run_id=runtime_state.session_id).log_event(
                source="service",
                event_type="service.recovered",
                payload={"reason": "codex_restart_succeeded"},
            )
            return restarted, 0, 0.0
        restart_failures += 1
        delay = codex_restart_delay(config, restart_failures)
        append_recovery_event(
            paths,
            f"codex restart failed -> backoff={delay:.1f}s",
            run_id=runtime_state.session_id,
        )
        return None, restart_failures, now + delay
    return codex, restart_failures, next_restart_at


def handle_authorized_message(
    text: str,
    auth: AuthState,
    runtime_state: RuntimeState,
    codex,
    telegram: TelegramClient,
    recorder: Recorder,
    session_store: SessionStore | None = None,
    topic_id: int | None = None,
    performance: PerformanceTracker | None = None,
    paths: AppPaths | None = None,
    config: Config | None = None,
    source_event_id: str | None = None,
    visible_topic_name: str | None = None,
) -> None:
    if not auth.telegram_chat_id:
        return
    callback_url = extract_login_callback_url(text)
    if runtime_state.codex_state == "AUTH_REQUIRED" and callback_url:
        if paths is not None:
            append_structured_event(
                paths,
                run_id=runtime_state.session_id,
                source="service",
                event_type="codex.login_callback.received",
                source_event_id=source_event_id,
                chat_id=auth.telegram_chat_id,
                topic_id=topic_id,
                payload={"callback_url": callback_url},
            )
        ok, detail = replay_login_callback(callback_url)
        if ok:
            if paths is not None:
                append_structured_event(
                    paths,
                    run_id=runtime_state.session_id,
                    source="service",
                    event_type="codex.login_callback.completed",
                    source_event_id=source_event_id,
                    chat_id=auth.telegram_chat_id,
                    topic_id=topic_id,
                    payload={"detail": detail},
                )
            send_telegram_message(
                telegram,
                auth.telegram_chat_id,
                "Codex login callback received. Waiting for Codex to finish sign-in.",
                topic_id=topic_id,
                performance=performance,
                category="status",
            )
        else:
            if paths is not None:
                append_structured_event(
                    paths,
                    run_id=runtime_state.session_id,
                    source="service",
                    event_type="codex.login_callback.failed",
                    source_event_id=source_event_id,
                    chat_id=auth.telegram_chat_id,
                    topic_id=topic_id,
                    payload={"detail": detail},
                )
            send_telegram_message(
                telegram,
                auth.telegram_chat_id,
                f"Codex login callback failed: {detail}",
                topic_id=topic_id,
                performance=performance,
                category="status",
            )
        return
    if runtime_state.codex_state == "AUTH_REQUIRED":
        codex_state = load_codex_server_state(paths) if paths is not None else None
        send_telegram_message(
            telegram,
            auth.telegram_chat_id,
            build_codex_login_required_message(codex_state),
            topic_id=topic_id,
            performance=performance,
            category="status",
        )
        return
    if text == "/status":
        model, reasoning = read_codex_cli_preferences()
        send_telegram_message(
            telegram,
            auth.telegram_chat_id,
            build_status_message(auth, runtime_state, session_store, topic_id, model, reasoning),
            topic_id=topic_id,
            performance=performance,
            category="status",
        )
        return
    if text == "/sessions":
        sessions = session_store.list_telegram_sessions(auth, topic_id) if session_store is not None else []
        if not sessions:
            send_telegram_message(
                telegram,
                auth.telegram_chat_id,
                "No sessions yet.",
                topic_id=topic_id,
                performance=performance,
                category="status",
            )
            return
        lines = ["Sessions"]
        for session in sessions:
            lines.append(f"{session.session_id} status={session.status} thread={session.thread_id or 'none'}")
        send_telegram_message(
            telegram,
            auth.telegram_chat_id,
            "\n".join(lines),
            topic_id=topic_id,
            performance=performance,
            category="status",
        )
        return
    if text == "/new":
        if session_store is None:
            send_telegram_message(
                telegram,
                auth.telegram_chat_id,
                "Session store is not available.",
                topic_id=topic_id,
                performance=performance,
                category="status",
            )
            return
        prior = session_store.get_current_telegram_session(auth, topic_id)
        session = session_store.create_new_telegram_session(
            auth,
            topic_id,
            visible_topic_name=visible_topic_name,
        )
        session.attached = True
        session.thread_id = None
        session.active_turn_id = None
        session.pending_output_text = ""
        session.pending_output_updated_at = None
        session.last_completed_turn_id = None
        session.last_delivered_output_text = ""
        session.streaming_message_id = None
        session.streaming_message_ids = []
        session.thinking_message_id = None
        session.thinking_message_ids = []
        session.thinking_live_message_ids = {}
        session.thinking_live_texts = {}
        session.thinking_sent_texts = {}
        session.streaming_output_text = ""
        session.streaming_phase = ""
        session.thinking_message_text = ""
        session.thinking_history_text = ""
        session.thinking_history_order = []
        session.thinking_history_by_source = {}
        session.last_thinking_sent_text = ""
        session.status = "ACTIVE"
        session_store.save_session(session)
        if prior is not None:
            append_recovery_event(
                session_store.paths,
                f"session_detached_on_new {session_log_label(prior)} replacement_session_id={session.session_id}",
                session_id=prior.session_id,
                thread_id=prior.thread_id,
                turn_id=prior.active_turn_id or prior.last_completed_turn_id,
                chat_id=prior.transport_chat_id,
                topic_id=prior.transport_topic_id,
            )
        append_recovery_event(
            session_store.paths,
            f"session_attached_on_new {session_log_label(session)}",
            session_id=session.session_id,
            thread_id=session.thread_id,
            turn_id=session.active_turn_id or session.last_completed_turn_id,
            chat_id=session.transport_chat_id,
            topic_id=session.transport_topic_id,
        )
        send_telegram_message(
            telegram,
            auth.telegram_chat_id,
            f"Started new session {session.session_id}.",
            topic_id=topic_id,
            performance=performance,
            category="status",
            session_id=session.session_id,
        )
        return
    if text == "/sleep":
        if paths is None or config is None:
            send_telegram_message(
                telegram,
                auth.telegram_chat_id,
                "Sleep is not available.",
                topic_id=topic_id,
                performance=performance,
                category="status",
            )
            return
        current_local = datetime.now().astimezone()
        run_sleep(paths, config, current_local, config.sleep_hour_local)
        send_telegram_message(
            telegram,
            auth.telegram_chat_id,
            "Sleep completed.",
            topic_id=topic_id,
            performance=performance,
            category="status",
        )
        return
    approval_store = ApprovalStore(session_store.paths) if session_store is not None else None
    approve_id = parse_request_command(text, "/approve")
    if approve_id is not None:
        if codex is None or not hasattr(codex, "approve") or approval_store is None:
            send_telegram_message(
                telegram,
                auth.telegram_chat_id,
                "Approval handling is not available.",
                topic_id=topic_id,
                performance=performance,
                category="approval",
            )
            return
        approval = approval_store.get_pending(approve_id)
        if approval is None:
            send_telegram_message(
                telegram,
                auth.telegram_chat_id,
                f"No pending approval {approve_id}.",
                topic_id=topic_id,
                performance=performance,
                category="approval",
            )
            return
        codex.approve(approve_id)
        approval_store.mark(approve_id, "approved")
        send_telegram_message(
            telegram,
            auth.telegram_chat_id,
            f"Approved request {approve_id}.",
            topic_id=topic_id,
            performance=performance,
            category="approval",
        )
        return
    deny_id = parse_request_command(text, "/deny")
    if deny_id is not None:
        if codex is None or not hasattr(codex, "deny") or approval_store is None:
            send_telegram_message(
                telegram,
                auth.telegram_chat_id,
                "Approval handling is not available.",
                topic_id=topic_id,
                performance=performance,
                category="approval",
            )
            return
        approval = approval_store.get_pending(deny_id)
        if approval is None:
            send_telegram_message(
                telegram,
                auth.telegram_chat_id,
                f"No pending approval {deny_id}.",
                topic_id=topic_id,
                performance=performance,
                category="approval",
            )
            return
        codex.deny(deny_id)
        approval_store.mark(deny_id, "denied")
        send_telegram_message(
            telegram,
            auth.telegram_chat_id,
            f"Denied request {deny_id}.",
            topic_id=topic_id,
            performance=performance,
            category="approval",
        )
        return
    if text == "/stop":
        if codex is None:
            send_telegram_message(
                telegram,
                auth.telegram_chat_id,
                "No active turn to stop.",
                topic_id=topic_id,
                performance=performance,
                category="status",
            )
            return
        if not hasattr(codex, "interrupt"):
            send_telegram_message(
                telegram,
                auth.telegram_chat_id,
                "Stop is not supported by the current Codex runtime.",
                topic_id=topic_id,
                performance=performance,
                category="status",
            )
            return
        try:
            stopped = codex.interrupt(topic_id=topic_id, chat_id=auth.telegram_chat_id, user_id=auth.telegram_user_id)
        except TypeError:
            try:
                stopped = codex.interrupt(topic_id=topic_id)
            except TypeError:
                stopped = codex.interrupt()
        if stopped:
            send_telegram_message(
                telegram,
                auth.telegram_chat_id,
                "Stopped the active turn.",
                topic_id=topic_id,
                performance=performance,
                category="status",
            )
        else:
            send_telegram_message(
                telegram,
                auth.telegram_chat_id,
                "No active turn to stop.",
                topic_id=topic_id,
                performance=performance,
                category="status",
            )
        return
    if text == "/abort":
        if codex is None:
            send_telegram_message(
                telegram,
                auth.telegram_chat_id,
                "No active turn to abort.",
                topic_id=topic_id,
                performance=performance,
                category="status",
            )
            return
        if not hasattr(codex, "interrupt"):
            send_telegram_message(
                telegram,
                auth.telegram_chat_id,
                "Abort is not supported by the current Codex runtime.",
                topic_id=topic_id,
                performance=performance,
                category="status",
            )
            return
        try:
            stopped = codex.interrupt(topic_id=topic_id, chat_id=auth.telegram_chat_id, user_id=auth.telegram_user_id)
        except TypeError:
            try:
                stopped = codex.interrupt(topic_id=topic_id)
            except TypeError:
                stopped = codex.interrupt()
        if stopped:
            send_telegram_message(
                telegram,
                auth.telegram_chat_id,
                "Aborted the active turn.",
                topic_id=topic_id,
                performance=performance,
                category="status",
            )
        else:
            send_telegram_message(
                telegram,
                auth.telegram_chat_id,
                "No active turn to abort.",
                topic_id=topic_id,
                performance=performance,
                category="status",
            )
        return
    if session_store is not None:
        current = session_store.get_current_telegram_session(auth, topic_id)
        if current is not None and current.status == "RECOVERING_TURN":
            send_telegram_message(
                telegram,
                auth.telegram_chat_id,
                "Current session is recovering an in-flight turn. Wait for recovery, use /stop, or start fresh with /new.",
                topic_id=topic_id,
                performance=performance,
                category="status",
            )
            return
    if codex is None:
        send_telegram_message(
            telegram,
            auth.telegram_chat_id,
            "Codex is not ready yet.",
            topic_id=topic_id,
            performance=performance,
            category="status",
        )
        return
    tracked_session = None
    session_id: str | None = None
    recovered_from_stale_turn = False
    thinking_segment_snapshot: dict[str, object] | None = None
    trace_store = TraceStore(paths, run_id=runtime_state.session_id) if paths is not None else None
    if session_store is not None:
        tracked_session = session_store.get_or_create_telegram_session(
            auth,
            topic_id,
            visible_topic_name=visible_topic_name,
        )
        if _should_queue_follow_up_user_message(tracked_session):
            _queue_follow_up_user_message(session_store, tracked_session, text)
            if paths is not None:
                append_structured_event(
                    paths,
                    run_id=runtime_state.session_id,
                    source="service",
                    event_type="ai.request.queued",
                    session_id=tracked_session.session_id,
                    thread_id=tracked_session.thread_id,
                    turn_id=tracked_session.active_turn_id,
                    source_event_id=source_event_id,
                    chat_id=tracked_session.transport_chat_id or auth.telegram_chat_id,
                    topic_id=tracked_session.transport_topic_id,
                    payload={"text_preview": text[:160], "reason": "active_turn_finishing"},
                )
            return
        if tracked_session.active_turn_id and tracked_session.thread_id and not is_stale_active_turn(tracked_session):
            _cancel_queued_live_progress_operations(paths, tracked_session, include_typing=True)
            thinking_segment_snapshot = _capture_thinking_segment_snapshot(tracked_session)
            _archive_visible_thinking_segment(tracked_session)
        if not tracked_session.active_turn_id:
            if _has_preservable_visible_answer(tracked_session):
                _set_streaming_message_ids(tracked_session, [])
            else:
                _clear_streaming_messages(telegram, tracked_session.transport_chat_id or auth.telegram_chat_id, tracked_session)
            tracked_session.pending_output_text = ""
            tracked_session.pending_output_updated_at = None
            tracked_session.streaming_output_text = ""
            tracked_session.streaming_phase = ""
            tracked_session.thinking_message_text = ""
            tracked_session.thinking_history_text = ""
            tracked_session.thinking_history_order = []
            tracked_session.thinking_history_by_source = {}
            tracked_session.thinking_live_texts = {}
            tracked_session.thinking_sent_texts = {}
            tracked_session.thinking_message_ids = []
            tracked_session.thinking_live_message_ids = {}
            tracked_session.last_thinking_sent_text = ""
        session_store.save_session(tracked_session)
        session_id = tracked_session.session_id
        if trace_store is not None:
            trace_id = trace_store.start_trace(
                session_id=tracked_session.session_id,
                chat_id=tracked_session.transport_chat_id or auth.telegram_chat_id,
                topic_id=tracked_session.transport_topic_id,
                user_text=text,
                thread_id=tracked_session.thread_id,
                turn_id=tracked_session.active_turn_id,
                source_event_id=source_event_id,
            )
            tracked_session.current_trace_id = trace_id
            session_store.save_session(tracked_session)
            trace_store.log_event(
                source="service",
                event_type="session.resolved",
                trace_id=trace_id,
                session_id=tracked_session.session_id,
                thread_id=tracked_session.thread_id,
                turn_id=tracked_session.active_turn_id,
                chat_id=tracked_session.transport_chat_id or auth.telegram_chat_id,
                topic_id=tracked_session.transport_topic_id,
            )
            trace_store.log_event(
                source="telegram_inbound",
                event_type="telegram.update.bound_to_trace",
                trace_id=trace_id,
                session_id=tracked_session.session_id,
                thread_id=tracked_session.thread_id,
                turn_id=tracked_session.active_turn_id,
                source_event_id=source_event_id,
                chat_id=tracked_session.transport_chat_id or auth.telegram_chat_id,
                topic_id=tracked_session.transport_topic_id,
                payload={"source_event_id": source_event_id},
            )
            trace_store.log_event(
                source="service",
                event_type="ai.request.started",
                trace_id=trace_id,
                session_id=tracked_session.session_id,
                thread_id=tracked_session.thread_id,
                turn_id=tracked_session.active_turn_id,
                chat_id=tracked_session.transport_chat_id or auth.telegram_chat_id,
                topic_id=tracked_session.transport_topic_id,
                payload={"text_preview": text[:160]},
            )
        if performance is not None:
            performance.mark_turn_requested(tracked_session, topic_id=topic_id, text=text)
    send_attempts = [
        lambda: codex.send(
            text,
            topic_id=topic_id,
            chat_id=auth.telegram_chat_id,
            user_id=auth.telegram_user_id,
            visible_topic_name=visible_topic_name,
        ),
        lambda: codex.send(text, topic_id=topic_id, chat_id=auth.telegram_chat_id, user_id=auth.telegram_user_id),
        lambda: codex.send(text, topic_id=topic_id),
        lambda: codex.send(text),
    ]
    try:
        for attempt in send_attempts:
            try:
                send_result = attempt()
                recovered_from_stale_turn = bool(send_result)
                break
            except TypeError:
                continue
        else:
            raise RuntimeError("Codex runtime does not support a compatible send signature.")
    except Exception as exc:
        if tracked_session is not None and thinking_segment_snapshot is not None:
            _restore_thinking_segment_snapshot(tracked_session, thinking_segment_snapshot)
            if session_store is not None:
                session_store.save_session(tracked_session)
        publish_codex_request_error(
            session_store,
            auth,
            telegram,
            session=tracked_session if session_store is not None else None,
            topic_id=topic_id,
            error_text=str(exc),
            performance=performance,
        )
        reset_session_after_request_failure(
            session_store,
            auth,
            telegram=telegram,
            session=tracked_session if session_store is not None else None,
            topic_id=topic_id,
            error_text=str(exc),
            clear_messages=False,
        )
        if performance is not None and session_id is not None:
            performance.mark_turn_failed(session_id, error=str(exc))
        log_request_failure(trace_store, session_store, auth, topic_id, str(exc), session=tracked_session)
        return
    if recovered_from_stale_turn:
        send_telegram_message(
            telegram,
            auth.telegram_chat_id,
            "Something went wrong with the previous message. I recovered the session and restarted your request.",
            topic_id=topic_id,
            performance=performance,
            category="status",
        )
    recorder.record("telegram", text)
    if session_store is not None and auth.telegram_chat_id:
        thinking_session = session_store.get_current_telegram_session(auth, topic_id)
        if thinking_session is not None and thinking_session.active_turn_id:
            ensure_thinking_message(auth, telegram, thinking_session, performance=performance)
            session_store.save_session(thinking_session)


def process_telegram_update(
    update: dict,
    *,
    paths: AppPaths,
    config: Config,
    auth: AuthState,
    runtime: ServiceRuntime,
    runtime_state: RuntimeState,
    metadata,
    app_lock,
    telegram: TelegramClient,
    recorder: Recorder,
    codex,
    handle_output,
    start_codex_session_fn=start_codex_session,
    performance: PerformanceTracker | None = None,
):
    update_id = update.get("update_id")
    trace_store = TraceStore(paths, run_id=runtime_state.session_id)
    topic_id = extract_update_topic_id(update)
    visible_topic_name = extract_update_topic_name(update)
    message = update.get("message", {}) or {}
    chat_id = message.get("chat", {}).get("id")
    if isinstance(update_id, int):
        update_store = TelegramUpdateStore(paths)
        if not update_store.mark_processed(
            update_id,
            chat_id=int(chat_id) if isinstance(chat_id, int) else None,
            topic_id=topic_id,
            payload=update,
        ):
            trace_store.log_event(
                source="telegram_inbound",
                event_type="telegram.update.duplicate",
                source_event_id=str(update_id),
                chat_id=int(chat_id) if isinstance(chat_id, int) else None,
                topic_id=topic_id,
                payload={"update_id": update_id, "update": update},
            )
            return codex

    session_store = SessionStore(paths)
    user_id = message.get("from", {}).get("id")
    source_event_id = str(update_id) if isinstance(update_id, int) else None
    text = build_telegram_input_text(
        paths,
        telegram,
        message,
        trace_store=trace_store,
        source_event_id=source_event_id,
        chat_id=int(chat_id) if isinstance(chat_id, int) else None,
        topic_id=topic_id,
    )
    trace_store.log_event(
        source="telegram_inbound",
        event_type="telegram.update.received",
        source_event_id=str(update_id) if isinstance(update_id, int) else None,
        chat_id=int(chat_id) if isinstance(chat_id, int) else None,
        topic_id=topic_id,
        payload={"update_id": update_id, "text_preview": text[:160], "update": update},
    )
    if performance is not None and text:
        performance.mark_telegram_message_received(
            update_id=update_id if isinstance(update_id, int) else None,
            chat_id=int(chat_id) if isinstance(chat_id, int) else None,
            topic_id=topic_id,
            text=text,
        )
    ok, status = register_pairing_request(auth, update)
    save_json(paths.auth, auth.to_dict())
    if status == "already-paired":
        chat_id = update.get("message", {}).get("chat", {}).get("id")
        trace_store.log_event(
            source="telegram_inbound",
            event_type="telegram.pairing.rejected",
            source_event_id=source_event_id,
            chat_id=int(chat_id) if isinstance(chat_id, int) else None,
            topic_id=topic_id,
            payload={"reason": "already_paired"},
        )
        if chat_id:
            send_telegram_message(
                telegram,
                chat_id,
                "This bot is already paired to another chat.",
                topic_id=topic_id,
                performance=performance,
                category="pairing",
            )
        return codex
    if status == "code-issued":
        trace_store.log_event(
            source="telegram_inbound",
            event_type="telegram.pairing.requested",
            source_event_id=source_event_id,
            chat_id=auth.pending_chat_id,
            topic_id=topic_id,
            payload={"pending_user_id": auth.pending_user_id},
        )
        if auth.pending_chat_id and auth.pairing_code:
            send_telegram_message(
                telegram,
                auth.pending_chat_id,
                f"Pairing code: {auth.pairing_code}. Enter this code in the local Tele Cli terminal to authorize this chat.",
                topic_id=topic_id,
                performance=performance,
                category="pairing",
            )
        print(
            "Pairing requested. "
            f"chat_id={auth.pending_chat_id} user_id={auth.pending_user_id} code={auth.pairing_code}"
        )
        if isatty():
            if complete_pending_pairing(paths, auth, telegram, allow_empty=True) and codex is None:
                trace_store.log_event(
                    source="telegram_inbound",
                    event_type="telegram.pairing.completed",
                    source_event_id=source_event_id,
                    chat_id=auth.telegram_chat_id,
                    topic_id=topic_id,
                    payload={"user_id": auth.telegram_user_id},
                )
                codex = invoke_start_codex_session_fn(
                    start_codex_session_fn,
                    config,
                    auth,
                    runtime,
                    runtime_state,
                    metadata,
                    app_lock,
                    telegram,
                    handle_output,
                    performance,
                )
        return codex
    if not ok:
        return codex
    scoped_auth = scoped_auth_for_update(
        auth,
        chat_id=int(chat_id) if isinstance(chat_id, int) else None,
        user_id=int(user_id) if isinstance(user_id, int) else None,
    )

    if text == "/model":
        send_telegram_message(
            telegram,
            scoped_auth.telegram_chat_id,
            'Usage: /model <name>',
            topic_id=topic_id,
            performance=performance,
            category="status",
        )
        return codex
    model_value = parse_value_command(text, "/model")
    if model_value is not None:
        write_codex_cli_preferences(model=model_value)
        codex = restart_codex_runtime(
            paths=paths,
            config=config,
            auth=auth,
            runtime=runtime,
            runtime_state=runtime_state,
            metadata=metadata,
            app_lock=app_lock,
            telegram=telegram,
            handle_output=handle_output,
            codex=codex,
            start_codex_session_fn=start_codex_session_fn,
            performance=performance,
        )
        send_telegram_message(
            telegram,
            scoped_auth.telegram_chat_id,
            restart_status_text(model_value, "Model", codex),
            topic_id=topic_id,
            performance=performance,
            category="status",
        )
        return codex

    if text == "/reasoning":
        send_telegram_message(
            telegram,
            scoped_auth.telegram_chat_id,
            'Usage: /reasoning <minimal|low|medium|high|xhigh>',
            topic_id=topic_id,
            performance=performance,
            category="status",
        )
        return codex
    reasoning_value = parse_value_command(text, "/reasoning")
    if reasoning_value is not None:
        normalized_reasoning = reasoning_value.lower()
        allowed_reasoning = {"minimal", "low", "medium", "high", "xhigh"}
        if normalized_reasoning not in allowed_reasoning:
            send_telegram_message(
                telegram,
                scoped_auth.telegram_chat_id,
                "Reasoning must be one of: minimal, low, medium, high, xhigh.",
                topic_id=topic_id,
                performance=performance,
                category="status",
            )
            return codex
        write_codex_cli_preferences(reasoning=normalized_reasoning)
        codex = restart_codex_runtime(
            paths=paths,
            config=config,
            auth=auth,
            runtime=runtime,
            runtime_state=runtime_state,
            metadata=metadata,
            app_lock=app_lock,
            telegram=telegram,
            handle_output=handle_output,
            codex=codex,
            start_codex_session_fn=start_codex_session_fn,
            performance=performance,
        )
        send_telegram_message(
            telegram,
            scoped_auth.telegram_chat_id,
            restart_status_text(normalized_reasoning, "Reasoning", codex),
            topic_id=topic_id,
            performance=performance,
            category="status",
        )
        return codex

    if text in {"/status", "/sessions", "/new", "/stop", "/abort", "/sleep"} or text.startswith("/approve ") or text.startswith("/deny "):
        handle_authorized_message(
            text,
            scoped_auth,
            runtime_state,
            codex,
            telegram,
            recorder,
            session_store,
            topic_id,
            performance,
            paths=paths,
            config=config,
            source_event_id=source_event_id,
            visible_topic_name=visible_topic_name,
        )
        return codex

    if codex is None and is_auth_paired(auth):
        codex = invoke_start_codex_session_fn(
            start_codex_session_fn,
            config,
            auth,
            runtime,
            runtime_state,
            metadata,
            app_lock,
            telegram,
            handle_output,
            performance,
        )

    if text:
        handle_authorized_message(
            text,
            scoped_auth,
            runtime_state,
            codex,
            telegram,
            recorder,
            session_store,
            topic_id,
            performance,
            paths=paths,
            config=config,
            source_event_id=source_event_id,
            visible_topic_name=visible_topic_name,
        )
    return codex


def drain_codex_approvals(
    paths: AppPaths,
    auth: AuthState,
    telegram: TelegramClient,
    codex,
    performance: PerformanceTracker | None = None,
) -> None:
    if codex is None or not hasattr(codex, "poll_approval_request") or not auth.telegram_chat_id:
        return
    approval_store = ApprovalStore(paths)
    while True:
        approval = codex.poll_approval_request()
        if approval is None:
            break
        approval_store.add(approval)
        send_telegram_message(
            telegram,
            auth.telegram_chat_id,
            f"Approval needed {approval.request_id}: {approval.method}. Reply with /approve {approval.request_id} or /deny {approval.request_id}.",
            performance=performance,
            category="approval",
        )


def drain_codex_notifications(
    paths: AppPaths,
    auth: AuthState,
    telegram: TelegramClient,
    recorder: Recorder,
    codex,
    runtime: ServiceRuntime | None = None,
    runtime_state: RuntimeState | None = None,
    config: Config | None = None,
    performance: PerformanceTracker | None = None,
    max_notifications: int | None = None,
    notification_pump: CodexNotificationPump | None = None,
) -> int:
    if notification_pump is None and (codex is None or not hasattr(codex, "poll_notification")):
        return 0
    session_store = SessionStore(paths)
    trace_store = TraceStore(paths, run_id=runtime_state.session_id if runtime_state is not None else None)
    handled = 0
    while True:
        if max_notifications is not None and handled >= max_notifications:
            break
        if notification_pump is not None:
            try:
                notification = notification_pump.get_nowait()
            except queue.Empty:
                break
        else:
            notification = codex.poll_notification()
            if notification is None:
                break
        handled += 1
        method = notification.method
        params = notification.params or {}
        agent_message_phase = remember_agent_message_phase(method, params)
        notification_record = build_app_server_notification_record(method, params)
        if performance is not None:
            performance.mark_notification_received(method, params)
        session = resolve_notification_session(session_store, auth, params)
        live_output_allowed = (
            session is not None and _session_accepts_live_output_notification(session, extract_turn_id(params))
        )
        trace_store.log_event(
            source="app_server",
            event_type="app_server.notification",
            trace_id=getattr(session, "current_trace_id", None) if session is not None else None,
            session_id=session.session_id if session is not None else None,
            thread_id=params.get("threadId"),
            turn_id=params.get("turnId"),
            chat_id=session.transport_chat_id if session is not None else auth.telegram_chat_id,
            topic_id=session.transport_topic_id if session is not None else None,
            item_id=params.get("itemId"),
            payload=notification_record,
        )
        thinking_delta = extract_thinking_delta(method, params)
        if session is not None and thinking_delta is not None:
            if not live_output_allowed:
                continue
            thinking_source_key = derive_thinking_source_key(
                method,
                params,
                agent_message_phase=agent_message_phase,
                thinking_text=thinking_delta,
            )
            append_thinking_delta(
                auth,
                telegram,
                session,
                thinking_delta,
                source_key=thinking_source_key,
                performance=performance,
            )
            session_store.save_session(session)
            set_visible_thinking_message(
                auth,
                telegram,
                recorder,
                session_store,
                session,
                text=session.thinking_message_text,
                source_key=thinking_source_key,
                performance=performance,
            )
            continue
        if method in {
            "assistant/message.delta",
            "item/agentMessage/delta",
            "item/updated",
            "item/started",
            "item/completed",
            "turn/output",
        }:
            commentary_text = accumulate_agent_message_text(method, params) if agent_message_phase == "commentary" else None
            text = None if agent_message_phase == "commentary" else extract_assistant_text(params)
            thinking_text = extract_thinking_text(params)
            activity_text = extract_activity_text(method, params)
            if session is not None and not live_output_allowed and (
                commentary_text or text or thinking_text or activity_text
            ):
                continue
            if session is not None and commentary_text:
                reply_started = performance.mark_reply_started(session, trigger=method) if performance is not None else True
                if reply_started and getattr(session, "current_trace_id", None):
                    trace_store.log_event(
                        source="service",
                        event_type="ai.reply.started",
                        trace_id=session.current_trace_id,
                        session_id=session.session_id,
                        thread_id=session.thread_id,
                        turn_id=session.active_turn_id,
                        chat_id=session.transport_chat_id,
                        topic_id=session.transport_topic_id,
                        payload={"trigger": method},
                    )
                commentary_source_key = derive_thinking_source_key(
                    method,
                    params,
                    agent_message_phase=agent_message_phase,
                )
                set_visible_thinking_message(
                    auth,
                    telegram,
                    recorder,
                    session_store,
                    session,
                    text=commentary_text,
                    source_key=commentary_source_key,
                    performance=performance,
                    min_interval_seconds=COMMENTARY_STREAM_MIN_INTERVAL_SECONDS,
                )
            elif session is not None and text:
                if session.streaming_phase == "commentary":
                    session.streaming_phase = "answer"
                    replace_pending_output(session_store, session, text)
                    session.streaming_output_text = ""
                    session_store.save_session(session)
                    action, payload = ("replace", text)
                else:
                    session.streaming_phase = "answer"
                    completed_agent_message = (
                        method == "item/completed"
                        and isinstance(params.get("item"), dict)
                        and params["item"].get("type") == "agentMessage"
                        and bool(session.pending_output_text.strip() or session.streaming_output_text.strip())
                    )
                    if completed_agent_message:
                        action, payload = ("replace", text)
                    elif method == "item/agentMessage/delta":
                        action, payload = merge_streamed_agent_delta_text(session, text)
                    else:
                        action, payload = merge_incremental_assistant_text(session, text)
                if action != "ignore" and payload:
                    reply_started = performance.mark_reply_started(session, trigger=method) if performance is not None else True
                    if reply_started and getattr(session, "current_trace_id", None):
                        trace_store.log_event(
                            source="service",
                            event_type="ai.reply.started",
                            trace_id=session.current_trace_id,
                            session_id=session.session_id,
                            thread_id=session.thread_id,
                            turn_id=session.active_turn_id,
                            chat_id=session.transport_chat_id,
                            topic_id=session.transport_topic_id,
                            payload={"trigger": method},
                        )
                    if action == "replace":
                        replace_pending_output(session_store, session, payload)
                        session.streaming_output_text = ""
                        session.streaming_phase = "answer"
                        session_store.save_session(session)
                    else:
                        session_store.append_pending_output(session, payload)
                    if method == "assistant/message.partial":
                        maybe_stream_partial_output(
                            auth,
                            telegram,
                            recorder,
                            session_store,
                            session,
                            performance=performance,
                        )
            elif session is not None and thinking_text:
                thinking_source_key = derive_thinking_source_key(
                    method,
                    params,
                    thinking_text=thinking_text,
                )
                set_visible_thinking_message(
                    auth,
                    telegram,
                    recorder,
                    session_store,
                    session,
                    text=thinking_text,
                    source_key=thinking_source_key,
                    performance=performance,
                )
            elif session is not None and activity_text:
                activity_source_key = derive_thinking_source_key(
                    method,
                    params,
                    activity_text=activity_text,
                )
                set_visible_thinking_message(
                    auth,
                    telegram,
                    recorder,
                    session_store,
                    session,
                    text=activity_text,
                    source_key=activity_source_key,
                    performance=performance,
                )
            continue
        if session is not None:
            status_text = extract_event_driven_status(method, params)
            if status_text and live_output_allowed:
                status_source_key = derive_thinking_source_key(
                    method,
                    params,
                    status_text=status_text,
                )
                set_visible_thinking_message(
                    auth,
                    telegram,
                    recorder,
                    session_store,
                    session,
                    text=status_text,
                    source_key=status_source_key,
                    performance=performance,
                )
        if method in {"account/updated", "account/ready", "login/completed", "account/login/completed"}:
            account_payload = extract_account_payload(params)
            if account_payload is None:
                continue
            next_state = update_codex_auth_state(
                paths,
                account_payload=account_payload,
                runtime=runtime,
                runtime_state=runtime_state,
            )
            continue
        if method == "assistant/message.partial":
            text = extract_assistant_text(params)
            session = resolve_notification_session(session_store, auth, params)
            if session is not None:
                if not _session_accepts_live_output_notification(session, extract_turn_id(params)):
                    continue
                if text:
                    reply_started = performance.mark_reply_started(session, trigger=method) if performance is not None else True
                    if reply_started and getattr(session, "current_trace_id", None):
                        trace_store.log_event(
                            source="service",
                            event_type="ai.reply.started",
                            trace_id=session.current_trace_id,
                            session_id=session.session_id,
                            thread_id=session.thread_id,
                            turn_id=session.active_turn_id,
                            chat_id=session.transport_chat_id,
                            topic_id=session.transport_topic_id,
                            payload={"trigger": method},
                        )
                    session_store.append_pending_output(session, text)
                maybe_stream_partial_output(
                    auth,
                    telegram,
                    recorder,
                    session_store,
                    session,
                    performance=performance,
                )
            continue
        if method in {"turn/completed", "turn/failed"}:
            turn_id = extract_turn_id(params)
            if not turn_id:
                continue
            session = session_store.find_by_turn_id(str(turn_id))
            if session is None:
                completed = session_store.find_by_completed_turn_id(str(turn_id))
                if completed is not None:
                    continue
                continue
            if not session_store.is_recoverable(session):
                continue
            request_failed = method == "turn/failed" or turn_completed_with_error(params)
            assistant_text = extract_assistant_text(params)
            if not assistant_text:
                assistant_text = _read_thread_completion_text(codex, session.thread_id)
            if should_append_completion_text(session, assistant_text):
                action, payload = merge_incremental_assistant_text(session, assistant_text)
                if action != "ignore" and payload:
                    reply_started = performance.mark_reply_started(session, trigger=method) if performance is not None else True
                    if reply_started and getattr(session, "current_trace_id", None):
                        trace_store.log_event(
                            source="service",
                            event_type="ai.reply.started",
                            trace_id=session.current_trace_id,
                            session_id=session.session_id,
                            thread_id=session.thread_id,
                            turn_id=session.active_turn_id,
                            chat_id=session.transport_chat_id,
                            topic_id=session.transport_topic_id,
                            payload={"trigger": method},
                        )
                    if action == "replace":
                        session.pending_output_text = payload
                        session.pending_output_updated_at = utc_now()
                        session.streaming_output_text = ""
                        session.streaming_phase = ""
                        session_store.save_session(session)
                    else:
                        session_store.append_pending_output(session, payload)
            session.active_turn_id = None
            session.last_completed_turn_id = str(turn_id)
            session.status = "ACTIVE"
            session_store.save_session(session)
            if getattr(session, "current_trace_id", None):
                completed_trace_id = session.current_trace_id
                trace_store.log_event(
                    source="service",
                    event_type="ai.reply.finished",
                    trace_id=completed_trace_id,
                    session_id=session.session_id,
                    thread_id=session.thread_id,
                    turn_id=str(turn_id),
                    chat_id=session.transport_chat_id,
                    topic_id=session.transport_topic_id,
                    payload={"outcome": "failed" if request_failed else "completed"},
                )
                trace_store.complete_trace(
                    completed_trace_id,
                    outcome="failed" if request_failed else "completed",
                    thread_id=session.thread_id,
                    turn_id=str(turn_id),
                )
                session.current_trace_id = None
                session_store.save_session(session)
            if performance is not None:
                performance.mark_reply_finished(
                    session,
                    outcome="failed" if request_failed else "completed",
                )
            if request_failed:
                error_text = extract_codex_error_text(params) or "The request failed."
                publish_codex_request_error(
                    session_store,
                    auth,
                    telegram,
                    session=session,
                    topic_id=session.transport_topic_id,
                    error_text=error_text,
                    performance=performance,
                )
                reset_session_after_request_failure(
                    session_store,
                    auth,
                    telegram=telegram,
                    session=session,
                    topic_id=session.transport_topic_id,
                    error_text=error_text,
                    clear_messages=False,
                )
                continue
            if not session.pending_output_text.strip():
                final_stream_text = session.streaming_output_text.strip()
                should_finalize_existing_delivery = bool(_streaming_message_ids(session)) and bool(
                    _thinking_history_entries(session)
                )
                should_queue_background_final = (
                    delivery_manager_supports_background_queue()
                    and bool(_streaming_message_ids(session))
                    and bool(final_stream_text)
                )
                if final_stream_text and (should_finalize_existing_delivery or should_queue_background_final):
                    session.pending_output_text = final_stream_text or session.last_delivered_output_text
                    session.pending_output_updated_at = utc_now()
                    session_store.save_session(session)
                else:
                    if final_stream_text and final_stream_text != session.last_delivered_output_text:
                        session_store.mark_delivered_output(session, final_stream_text)
                        recorder.record("assistant", final_stream_text)
                    clear_thinking_message(auth, telegram, session_store, session, performance=performance)
                    if final_stream_text and final_stream_text != session.last_delivered_output_text:
                        _set_streaming_message_ids(session, [])
                    else:
                        _clear_streaming_messages(telegram, session.transport_chat_id or auth.telegram_chat_id, session)
                    session.streaming_output_text = ""
                    session.streaming_phase = ""
                    session.thinking_message_text = ""
                    session.thinking_history_text = ""
                    session.thinking_history_order = []
                    session.thinking_history_by_source = {}
                    session.last_thinking_sent_text = ""
                    session.thinking_message_ids = []
                    session.thinking_live_message_ids = {}
                    session.thinking_live_texts = {}
                    session.thinking_sent_texts = {}
                    session_store.save_session(session)
                    continue
            flush_buffer(
                session.session_id,
                auth,
                telegram,
                recorder,
                session_store,
                mark_agent=True,
                queue_only=True,
                performance=performance,
            )
            continue
        if method in {"thread/updated", "thread/resumed"}:
            thread_id = params.get("threadId")
            if not thread_id:
                continue
            session = session_store.find_by_thread_id(str(thread_id))
            if session is None:
                session = session_store.get_current_telegram_session(auth)
                if session is None or session.thread_id is not None:
                    continue
                session.thread_id = str(thread_id)
                session_store.save_session(session)
    return handled


def bootstrap_paired_codex(
    *,
    paths: AppPaths,
    config: Config,
    auth: AuthState,
    runtime: ServiceRuntime,
    runtime_state: RuntimeState,
    metadata,
    app_lock,
    telegram: TelegramClient,
    handle_output,
    codex,
    start_codex_session_fn,
    performance: PerformanceTracker | None = None,
):
    if not is_auth_paired(auth) or codex is not None:
        return codex
    return invoke_start_codex_session_fn(
        start_codex_session_fn,
        config,
        auth,
        runtime,
        runtime_state,
        metadata,
        app_lock,
        telegram,
        handle_output,
        performance,
    )


class CodexNotificationPump:
    def __init__(self, *, maxsize: int = CODEX_NOTIFICATION_QUEUE_MAX_SIZE):
        self._queue: queue.Queue[object] = queue.Queue(maxsize=maxsize)
        self._codex = None
        self._codex_lock = threading.Lock()
        self._wake_event = threading.Event()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self, codex=None) -> None:
        self.set_codex(codex)
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run_loop, name="codex-notification-pump", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._wake_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def set_codex(self, codex) -> None:
        with self._codex_lock:
            self._codex = codex
        self._wake_event.set()

    def get_nowait(self):
        return self._queue.get_nowait()

    def _current_codex(self):
        with self._codex_lock:
            return self._codex

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            codex = self._current_codex()
            if codex is None or not hasattr(codex, "poll_notification"):
                self._wake_event.wait(CODEX_NOTIFICATION_POLL_IDLE_SECONDS)
                self._wake_event.clear()
                continue
            try:
                notification = codex.poll_notification()
            except Exception:
                if self._stop_event.is_set():
                    break
                self._wake_event.wait(CODEX_NOTIFICATION_POLL_IDLE_SECONDS)
                self._wake_event.clear()
                continue
            if notification is None:
                self._wake_event.wait(CODEX_NOTIFICATION_POLL_IDLE_SECONDS)
                self._wake_event.clear()
                continue
            while not self._stop_event.is_set():
                try:
                    self._queue.put(notification, timeout=0.1)
                    break
                except queue.Full:
                    continue


def run_service(
    paths: AppPaths,
    start_codex_session_fn=start_codex_session,
    conflict_choices: ServiceConflictChoices | None = None,
) -> None:
    paths.root.mkdir(parents=True, exist_ok=True)
    config = load_json(paths.config, Config.from_dict)
    auth = load_json(paths.auth, AuthState.from_dict)
    if not config or not auth:
        raise RuntimeError("Run setup first.")
    ensure_instruction_files(paths)
    write_codex_cli_preferences(
        approval_policy=config.approval_policy,
        sandbox_mode=config.sandbox_mode,
    )

    app_lock, metadata = prepare_service_lock(paths, choices=conflict_choices)

    runtime_state = RuntimeState(
        session_id=str(uuid.uuid4()),
        service_state="RUNNING",
        codex_state="STOPPED",
        telegram_state="STOPPED",
        recorder_state="STOPPED",
        debug_state="STOPPED",
    )
    runtime = ServiceRuntime(runtime_state)
    run_store = ServiceRunStore(paths)
    run_store.start(run_id=runtime_state.session_id, pid=getattr(metadata, "pid", None))
    start_async_log_prune(paths, run_id=runtime_state.session_id)
    log_trace_store = TraceStore(paths, run_id=runtime_state.session_id)
    log_trace_store.log_event(source="service", event_type="service.starting")
    recorder = Recorder(paths.terminal_log, trace_store=log_trace_store)
    performance = PerformanceTracker(paths.performance_log, trace_store=log_trace_store)
    debug = DebugMirror()
    telegram = TelegramClient(auth.bot_token)
    install_delivery_manager(paths, telegram, run_id=runtime_state.session_id)
    session_store = SessionStore(paths)
    approval_store = ApprovalStore(paths)
    if start_codex_session_fn is start_codex_session:
        start_codex_session_fn = make_app_server_start_fn(paths, default_transport_factory)

    runtime.start_recorder()
    recorder.start()
    runtime.start_debug()
    debug.start()
    runtime.start_telegram()
    stale_approvals = approval_store.mark_all_pending_stale()

    def handle_output(source: str, line: str) -> None:
        recorder.record(source, line)
        debug.emit(source, line)
        runtime_state.last_output_at = utc_now()
        save_runtime_state(paths, runtime_state)

    codex = None
    last_typing_sent_at: datetime | None = None
    codex_restart_failures = 0
    next_codex_restart_at = 0.0
    notification_pump = CodexNotificationPump()
    updates_queue: queue.Queue[dict] = queue.Queue()
    stop_event = threading.Event()
    poll_gate = threading.Event()
    poll_gate.set()
    last_sleep_check = 0.0
    codex = bootstrap_paired_codex(
        paths=paths,
        config=config,
        auth=auth,
        runtime=runtime,
        runtime_state=runtime_state,
        metadata=metadata,
        app_lock=app_lock,
        telegram=telegram,
        handle_output=handle_output,
        codex=codex,
        start_codex_session_fn=start_codex_session_fn,
        performance=performance,
    )
    if codex is None and is_auth_paired(auth):
        codex_restart_failures = 1
        next_codex_restart_at = time.monotonic() + codex_restart_delay(config, codex_restart_failures)
    notification_pump.start(codex)
    save_runtime_state(paths, runtime_state)
    append_recovery_event(
        paths,
        f"service started session_id={runtime_state.session_id}",
        run_id=runtime_state.session_id,
    )
    log_trace_store.log_event(source="service", event_type="service.started")
    telegram_thread = start_telegram_polling_thread(
        paths=paths,
        config=config,
        telegram=telegram,
        runtime_state=runtime_state,
        update_queue=updates_queue,
        stop_event=stop_event,
        poll_gate=poll_gate,
    )
    threading.Event().wait(SERVICE_STARTUP_WAIT_SECONDS)
    startup_now = datetime.now().astimezone()
    if has_pending_sleep_work(paths) and should_run_sleep(paths, startup_now, config.sleep_hour_local):
        poll_gate.set()
        try:
            run_sleep(paths, config, startup_now, config.sleep_hour_local)
        except Exception as exc:
            append_recovery_event(paths, f"sleep failed on startup -> {exc}", run_id=runtime_state.session_id)

    try:
        if has_pending_pairing(auth) and isatty():
            complete_pending_pairing(paths, auth, telegram, allow_empty=True)
            codex = bootstrap_paired_codex(
                paths=paths,
                config=config,
                auth=auth,
                runtime=runtime,
                runtime_state=runtime_state,
                metadata=metadata,
                app_lock=app_lock,
                telegram=telegram,
                handle_output=handle_output,
                codex=codex,
                start_codex_session_fn=start_codex_session_fn,
                performance=performance,
            )
        while True:
            if not telegram_thread.is_alive():
                append_recovery_event(paths, "telegram poll thread stopped -> restarting", run_id=runtime_state.session_id)
                append_telegram_poll_log(paths, "thread_restarting")
                telegram_thread = start_telegram_polling_thread(
                    paths=paths,
                    config=config,
                    telegram=telegram,
                    runtime_state=runtime_state,
                    update_queue=updates_queue,
                    stop_event=stop_event,
                    poll_gate=poll_gate,
                )
            poll_gate.clear()
            processed_updates = False
            while True:
                try:
                    update = updates_queue.get_nowait()
                except queue.Empty:
                    break
                processed_updates = True
                codex = process_telegram_update(
                    update,
                    paths=paths,
                    config=config,
                    auth=auth,
                    runtime=runtime,
                    runtime_state=runtime_state,
                    metadata=metadata,
                    app_lock=app_lock,
                    telegram=telegram,
                    recorder=recorder,
                    codex=codex,
                    handle_output=handle_output,
                    start_codex_session_fn=start_codex_session_fn,
                    performance=performance,
                )
                notification_pump.set_codex(codex)
                if not updates_queue.empty():
                    continue
                drain_codex_approvals(paths, auth, telegram, codex, performance)
                drain_codex_notifications(
                    paths,
                    auth,
                    telegram,
                    recorder,
                    codex,
                    runtime,
                    runtime_state,
                    config,
                    performance,
                    max_notifications=100,
                    notification_pump=notification_pump,
                )
                flush_idle_partial_outputs(
                    paths,
                    auth,
                    telegram,
                    recorder,
                    session_store,
                    idle_seconds=config.partial_flush_idle_seconds,
                    performance=performance,
                )
                reconcile_pending_final_deliveries(
                    paths,
                    auth,
                    telegram,
                    recorder,
                    session_store,
                    performance=performance,
                )
                dispatch_queued_user_inputs(
                    paths,
                    auth,
                    runtime_state,
                    telegram,
                    recorder,
                    session_store,
                    codex,
                    config=config,
                    performance=performance,
                )
                last_typing_sent_at = maybe_send_typing_indicator(
                    paths,
                    auth,
                    telegram,
                    session_store,
                    interval_seconds=config.typing_indicator_interval_seconds,
                    last_sent_at=last_typing_sent_at,
                    performance=performance,
                )
                if codex is not None:
                    maybe_refresh_thinking_message(
                        paths,
                        auth,
                        telegram,
                        session_store,
                        recorder=recorder,
                        performance=performance,
                    )
                codex, codex_restart_failures, next_codex_restart_at = maintain_codex_runtime(
                    paths=paths,
                    config=config,
                    auth=auth,
                    runtime=runtime,
                    runtime_state=runtime_state,
                    metadata=metadata,
                    app_lock=app_lock,
                    telegram=telegram,
                    handle_output=handle_output,
                    codex=codex,
                    start_codex_session_fn=start_codex_session_fn,
                    restart_failures=codex_restart_failures,
                    next_restart_at=next_codex_restart_at,
                    performance=performance,
                )
                notification_pump.set_codex(codex)
            if processed_updates:
                poll_gate.set()
                threading.Event().wait(SERVICE_LOOP_YIELD_SECONDS)
                continue
            if time.monotonic() - last_sleep_check >= 30.0:
                last_sleep_check = time.monotonic()
                current_local = datetime.now().astimezone()
                if has_pending_sleep_work(paths) and should_run_sleep(paths, current_local, config.sleep_hour_local):
                    poll_gate.set()
                    try:
                        run_sleep(paths, config, current_local, config.sleep_hour_local)
                    except Exception as exc:
                        append_recovery_event(
                            paths,
                            f"sleep failed during service loop -> {exc}",
                            run_id=runtime_state.session_id,
                        )
            drain_codex_approvals(paths, auth, telegram, codex, performance)
            drain_codex_notifications(
                paths,
                auth,
                telegram,
                recorder,
                codex,
                runtime,
                runtime_state,
                config,
                performance,
                max_notifications=100,
                notification_pump=notification_pump,
            )
            flush_idle_partial_outputs(
                paths,
                auth,
                telegram,
                recorder,
                session_store,
                idle_seconds=config.partial_flush_idle_seconds,
                performance=performance,
            )
            reconcile_pending_final_deliveries(
                paths,
                auth,
                telegram,
                recorder,
                session_store,
                performance=performance,
            )
            dispatch_queued_user_inputs(
                paths,
                auth,
                runtime_state,
                telegram,
                recorder,
                session_store,
                codex,
                config=config,
                performance=performance,
            )
            last_typing_sent_at = maybe_send_typing_indicator(
                paths,
                auth,
                telegram,
                session_store,
                interval_seconds=config.typing_indicator_interval_seconds,
                last_sent_at=last_typing_sent_at,
                performance=performance,
            )
            if codex is not None:
                maybe_refresh_thinking_message(
                    paths,
                    auth,
                    telegram,
                    session_store,
                    recorder=recorder,
                    performance=performance,
                )
            codex, codex_restart_failures, next_codex_restart_at = maintain_codex_runtime(
                paths=paths,
                config=config,
                auth=auth,
                runtime=runtime,
                runtime_state=runtime_state,
                metadata=metadata,
                app_lock=app_lock,
                telegram=telegram,
                handle_output=handle_output,
                codex=codex,
                start_codex_session_fn=start_codex_session_fn,
                restart_failures=codex_restart_failures,
                next_restart_at=next_codex_restart_at,
                performance=performance,
            )
            notification_pump.set_codex(codex)
            poll_gate.set()
            threading.Event().wait(SERVICE_LOOP_YIELD_SECONDS)
            if not updates_queue.empty():
                continue
            time.sleep(service_tick_seconds(config))
    finally:
        threading.Event().wait(SERVICE_LOOP_YIELD_SECONDS)
        log_trace_store.log_event(source="service", event_type="service.stopping")
        stop_event.set()
        poll_gate.set()
        telegram_thread.join(timeout=SERVICE_THREAD_JOIN_TIMEOUT_SECONDS)
        notification_pump.stop()
        if codex is not None:
            codex.stop()
            runtime.stop_codex()
        recorder.stop()
        debug.stop()
        app_lock.clear()
        uninstall_delivery_manager()
        run_store.stop(run_id=runtime_state.session_id, exit_reason="service_stopped")
        append_recovery_event(paths, "service stopped", run_id=runtime_state.session_id)
        log_trace_store.log_event(source="service", event_type="service.stopped")
