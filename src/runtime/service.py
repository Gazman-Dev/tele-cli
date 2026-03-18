from __future__ import annotations

from datetime import datetime, timezone
import inspect
import json
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
from core.logging_utils import append_recovery_log
from core.models import AuthState, CodexServerState, Config, RuntimeState, utc_now
from core.paths import AppPaths
from core.state_versions import load_versioned_state, save_versioned_state
from integrations.telegram import (
    TelegramClient,
    TelegramError,
    describe_pairing,
    has_pending_pairing,
    is_auth_paired,
    register_pairing_request,
)
from setup.setup_flow import complete_pending_pairing
from .app_server_runtime import default_transport_factory, derive_codex_state, make_app_server_start_fn
from .approval_store import ApprovalStore
from .codex_cli_config import read_codex_cli_preferences, write_codex_cli_preferences
from .control import ServiceConflictChoices, isatty, prepare_service_lock, reset_auth, start_codex_session
from .performance import PerformanceTracker, edit_telegram_message, send_telegram_message
from .recorder import Recorder
from .runtime import ServiceRuntime
from .session_store import SessionStore
from .telegram_update_store import TelegramUpdateStore


LOCAL_AUTH_CALLBACK_RE = re.compile(r"https?://(?:localhost|127\.0\.0\.1):1455/auth/callback\?[^\s]+", re.IGNORECASE)
TELEGRAM_TEXT_LIMIT = 4000


def service_tick_seconds(config: Config) -> float:
    configured = max(config.poll_interval_seconds, 0.0)
    if configured == 0.0:
        return 0.05
    return max(min(configured, 0.1), 0.01)


def append_app_server_notification_log(paths: AppPaths, method: str, params: dict) -> None:
    paths.root.mkdir(parents=True, exist_ok=True)
    item = params.get("item") if isinstance(params.get("item"), dict) else {}
    record = {
        "timestamp": utc_now(),
        "method": method,
        "thread_id": params.get("threadId") or params.get("thread_id"),
        "turn_id": params.get("turnId") or params.get("turn_id"),
        "item_type": item.get("type"),
    }
    with paths.root.joinpath("app_server_notifications.log").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def split_telegram_text(text: str, limit: int = TELEGRAM_TEXT_LIMIT) -> list[str]:
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
        while not stop_event.is_set():
            if not poll_gate.is_set() or not update_queue.empty():
                stop_event.wait(0.01)
                continue
            now = time.monotonic()
            if now < next_poll_at:
                stop_event.wait(min(next_poll_at - now, 0.1))
                continue
            try:
                updates = telegram.get_updates(offset=offset, timeout=20)
                if runtime_state.telegram_state != "RUNNING":
                    runtime_state.telegram_state = "RUNNING"
                    save_json(paths.runtime, runtime_state.to_dict())
                telegram_failures = 0
                next_poll_at = 0.0
            except TelegramError as exc:
                telegram_failures += 1
                delay = telegram_retry_delay(config, telegram_failures)
                next_poll_at = time.monotonic() + delay
                runtime_state.telegram_state = "BACKOFF"
                save_json(paths.runtime, runtime_state.to_dict())
                append_recovery_log(paths.recovery_log, f"telegram poll failed -> backoff={delay:.1f}s error={exc}")
                continue
            for update in updates:
                update_id = update.get("update_id")
                if isinstance(update_id, int):
                    offset = update_id + 1
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
    save_json(paths.runtime, runtime_state.to_dict())
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


def extract_update_topic_id(update: dict) -> int | None:
    message = update.get("message") or {}
    topic_id = message.get("message_thread_id")
    return int(topic_id) if isinstance(topic_id, int) else None


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


def _extract_search_hint(arguments: object) -> str | None:
    if not isinstance(arguments, dict):
        return None
    for key in ("query", "q", "searchTerm", "term", "prompt"):
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            return _shorten_activity_text(value.strip(), limit=60)
    return None


def extract_activity_text(method: str, params: dict) -> str | None:
    item = params.get("item")
    if not isinstance(item, dict):
        return None
    item_type = str(item.get("type") or "")
    if item_type == "commandExecution":
        command = item.get("command")
        if isinstance(command, str) and command.strip():
            return f"Running command: {_shorten_activity_text(command.strip())}"
        return "Running command..."
    if item_type == "mcpToolCall":
        server = item.get("server")
        tool = item.get("tool")
        if isinstance(server, str) and server and isinstance(tool, str) and tool:
            return f"Using {server}/{tool}..."
        if isinstance(tool, str) and tool:
            return f"Using tool: {tool}..."
        return "Using external tool..."
    if item_type == "dynamicToolCall":
        tool = item.get("tool")
        arguments = item.get("arguments")
        if isinstance(tool, str) and tool:
            lowered = tool.lower()
            hint = _extract_search_hint(arguments)
            if "search" in lowered and hint:
                return f"Searching: {hint}"
            return f"Using tool: {tool}..."
        return "Using tool..."
    if item_type == "collabAgentToolCall":
        tool = str(item.get("tool") or "")
        status = str(item.get("status") or "")
        if tool == "spawnAgent":
            return "Spawning helper agent..."
        if tool in {"wait", "closeAgent", "resumeAgent", "sendInput"}:
            return "Coordinating helper agent..."
        if status:
            return "Coordinating helper agents..."
        return "Using helper agent..."
    if item_type == "fileChange":
        return "Applying file changes..."
    if item_type == "plan":
        return "Planning next steps..."
    if item_type == "search":
        query = _extract_search_hint(item)
        if query:
            return f"Searching: {query}"
        return "Searching..."
    return None


def extract_event_driven_status(method: str, params: dict) -> str | None:
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
                    return "Active"
                label = _humanize_status_label(status_type)
                if label:
                    return label
        status = str(status_value or "").lower()
        if status in {"running", "in_progress", "working"}:
            return "Working..."
    if method == "thread/tokenUsage/updated":
        return "Finalizing answer..."
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
    combined = f"{session.streaming_output_text}{session.pending_output_text}".strip()
    if not combined:
        return
    if session.streaming_output_text:
        last_sent_at = parse_utc_timestamp(session.last_agent_message_at)
        now = now or datetime.now(timezone.utc)
        if last_sent_at is not None and (now - last_sent_at).total_seconds() < min_interval_seconds:
            return
    flush_buffer(
        session.session_id,
        auth,
        telegram,
        recorder,
        session_store,
        mark_agent=False,
        performance=performance,
    )


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
        return "Thinking..."
    started_at = parse_utc_timestamp(session.last_user_message_at)
    if started_at is None:
        return "Thinking..."
    elapsed = max((datetime.now(timezone.utc) - started_at).total_seconds(), 0.0)
    if elapsed < 4.0:
        return "Thinking..."
    if elapsed < 12.0:
        return "Still thinking..."
    if elapsed < 24.0:
        return "Still thinking. Working through the request..."
    return "Still thinking. This one is taking longer than usual..."


def is_default_thinking_text(text: str | None) -> bool:
    if not text:
        return True
    return text in {
        "Thinking...",
        "Still thinking...",
        "Still thinking. Working through the request...",
        "Still thinking. This one is taking longer than usual...",
    }


def ensure_thinking_message(
    auth: AuthState,
    telegram: TelegramClient,
    session,
    *,
    text: str | None = None,
    performance: PerformanceTracker | None = None,
) -> None:
    if not auth.telegram_chat_id:
        return
    if session.streaming_output_text:
        return
    display_text = text or default_thinking_text(session)
    if session.streaming_message_id is not None:
        if session.thinking_message_text == display_text:
            return
        edit_telegram_message(
            telegram,
            auth.telegram_chat_id,
            session.streaming_message_id,
            display_text,
            performance=performance,
            category="assistant_placeholder",
            session_id=session.session_id,
            thread_id=session.thread_id,
            turn_id=session.active_turn_id,
        )
        session.thinking_message_text = display_text
        return
    message_id = send_telegram_message(
        telegram,
        auth.telegram_chat_id,
        display_text,
        performance=performance,
        category="assistant_placeholder",
        session_id=session.session_id,
        thread_id=session.thread_id,
        turn_id=session.active_turn_id,
    )
    session.streaming_message_id = message_id
    session.thinking_message_text = display_text


def maybe_refresh_thinking_message(
    paths: AppPaths,
    auth: AuthState,
    telegram: TelegramClient,
    session_store: SessionStore,
    *,
    performance: PerformanceTracker | None = None,
) -> None:
    if not auth.telegram_chat_id:
        return
    if ApprovalStore(paths).pending():
        return
    current = session_store.get_current_telegram_session(auth)
    if current is None or not current.attached or not current.active_turn_id:
        return
    if current.pending_output_text or current.streaming_output_text:
        return
    if current.streaming_message_id is None and not current.thinking_message_text:
        return
    if current.thinking_message_text and not is_default_thinking_text(current.thinking_message_text):
        return
    next_text = default_thinking_text(current)
    if current.thinking_message_text == next_text:
        return
    ensure_thinking_message(auth, telegram, current, text=next_text, performance=performance)
    session_store.save_session(current)


def append_thinking_delta(
    auth: AuthState,
    telegram: TelegramClient,
    session,
    delta: str,
    *,
    performance: PerformanceTracker | None = None,
) -> None:
    if not delta:
        return
    if session.thinking_message_text:
        separator = ""
        if (
            not session.thinking_message_text.endswith((" ", "\n"))
            and not delta.startswith((" ", "\n", ".", ",", ";", ":", "!", "?", ")"))
        ):
            separator = " "
        next_text = f"{session.thinking_message_text}{separator}{delta}"
    else:
        next_text = delta
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
    if "status" in params or "state" in params:
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
    persisted = load_versioned_state(paths.codex_server, CodexServerState.from_dict)
    if persisted is None:
        persisted = CodexServerState(transport="stdio://", initialized=True)
    account_info = account_payload.get("account") if isinstance(account_payload.get("account"), dict) else {}
    persisted.account_status = account_payload.get("status") or account_payload.get("state")
    persisted.account_type = (
        account_payload.get("accountType")
        or account_payload.get("type")
        or account_info.get("accountType")
        or account_info.get("type")
    )
    persisted.auth_required = derive_codex_state(account_payload) == "AUTH_REQUIRED"
    if not persisted.auth_required:
        persisted.login_url = None
        persisted.login_type = None
    save_versioned_state(paths.codex_server, persisted.to_dict())
    next_state = derive_codex_state(account_payload)
    if runtime is not None and runtime_state is not None:
        runtime.set_codex_state(next_state)
        save_json(paths.runtime, runtime_state.to_dict())
    return next_state


def resolve_notification_session(
    session_store: SessionStore,
    auth: AuthState,
    params: dict,
):
    thread_id = params.get("threadId")
    if thread_id:
        session = session_store.find_by_thread_id(str(thread_id))
        if session is not None and session_store.is_recoverable(session):
            return session
        return None
    turn_id = params.get("turnId")
    if turn_id:
        session = session_store.find_by_turn_id(str(turn_id))
        if session is not None and session_store.is_recoverable(session):
            return session
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
    performance: PerformanceTracker | None = None,
) -> None:
    session = next((item for item in session_store.load().sessions if item.session_id == session_id), None)
    if session is None:
        return
    pending_text = session.pending_output_text
    if not pending_text.strip():
        return
    text = pending_text.strip()
    if session.streaming_output_text:
        streamed_text = session.streaming_output_text.strip()
        if text.startswith(streamed_text):
            text = text
        else:
            text = f"{session.streaming_output_text}{pending_text}".strip()
    if not session.attached or not auth.telegram_chat_id:
        append_recovery_log(
            session_store.paths.recovery_log,
            f"hidden_session_output_consumed {session_log_label(session)} delivered_to_telegram=false",
        )
        if mark_agent:
            session_store.mark_agent_message(session)
        session_store.consume_pending_output(session)
        pruned = session_store.prune_detached_sessions()
        if pruned:
            append_recovery_log(session_store.paths.recovery_log, f"detached_sessions_pruned count={pruned}")
        return
    if text == session.last_delivered_output_text:
        if mark_agent:
            session.streaming_message_id = None
            session.streaming_output_text = ""
            session.thinking_message_text = ""
            session_store.save_session(session)
        session_store.consume_pending_output(session)
        return
    chunks = split_telegram_text(text)
    if not chunks:
        return
    context = {
        "performance": performance,
        "category": "assistant_output",
        "session_id": session.session_id,
        "thread_id": session.thread_id,
        "turn_id": session.active_turn_id or session.last_completed_turn_id,
    }
    try:
        if session.streaming_message_id is not None:
            edit_telegram_message(
                telegram,
                auth.telegram_chat_id,
                session.streaming_message_id,
                chunks[0],
                **context,
            )
        else:
            message_id = send_telegram_message(
                telegram,
                auth.telegram_chat_id,
                chunks[0],
                **context,
            )
            if not mark_agent:
                session.streaming_message_id = message_id
                session_store.save_session(session)
        for chunk in chunks[1:]:
            send_telegram_message(
                telegram,
                auth.telegram_chat_id,
                chunk,
                **context,
            )
    except TelegramError:
        if session.streaming_message_id is not None:
            try:
                edit_telegram_message(
                    telegram,
                    auth.telegram_chat_id,
                    session.streaming_message_id,
                    "Reply continues below.",
                    **context,
                )
            except TelegramError:
                pass
        for chunk in chunks:
            send_telegram_message(
                telegram,
                auth.telegram_chat_id,
                chunk,
                **context,
            )
        session.streaming_message_id = None
        session_store.save_session(session)
    recorder.record("assistant", text)
    session.streaming_output_text = text
    session.thinking_message_text = ""
    session_store.mark_delivered_output(session, text)
    session_store.mark_agent_message(session)
    if mark_agent:
        session.streaming_message_id = None
        session.streaming_output_text = ""
        session.thinking_message_text = ""
        session_store.save_session(session)
    session_store.consume_pending_output(session)
    pruned = session_store.prune_detached_sessions()
    if pruned:
        append_recovery_log(session_store.paths.recovery_log, f"detached_sessions_pruned count={pruned}")


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
) -> datetime | None:
    if interval_seconds <= 0 or not auth.telegram_chat_id:
        return last_sent_at
    if ApprovalStore(paths).pending():
        return last_sent_at
    current = session_store.get_current_telegram_session(auth)
    if current is None or not current.attached or not current.active_turn_id:
        return last_sent_at
    now = now or datetime.now(timezone.utc)
    if last_sent_at is not None and (now - last_sent_at).total_seconds() < interval_seconds:
        return last_sent_at
    if hasattr(telegram, "send_typing"):
        telegram.send_typing(auth.telegram_chat_id)
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
        save_json(paths.runtime, runtime_state.to_dict())
        append_recovery_log(paths.recovery_log, f"codex child exited -> restart backoff={delay:.1f}s")
        if auth.telegram_chat_id:
            send_telegram_message(
                telegram,
                auth.telegram_chat_id,
                f"Codex App Server stopped. Restarting in {delay:.1f}s. Telegram remains available.",
                performance=performance,
                category="startup_notification",
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
            append_recovery_log(paths.recovery_log, "codex restart succeeded")
            return restarted, 0, 0.0
        restart_failures += 1
        delay = codex_restart_delay(config, restart_failures)
        append_recovery_log(paths.recovery_log, f"codex restart failed -> backoff={delay:.1f}s")
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
) -> None:
    if not auth.telegram_chat_id:
        return
    callback_url = extract_login_callback_url(text)
    if runtime_state.codex_state == "AUTH_REQUIRED" and callback_url:
        ok, detail = replay_login_callback(callback_url)
        if ok:
            send_telegram_message(
                telegram,
                auth.telegram_chat_id,
                "Codex login callback received. Waiting for Codex to finish sign-in.",
                performance=performance,
                category="status",
            )
        else:
            send_telegram_message(
                telegram,
                auth.telegram_chat_id,
                f"Codex login callback failed: {detail}",
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
                performance=performance,
                category="status",
            )
            return
        prior = session_store.get_current_telegram_session(auth, topic_id)
        session = session_store.create_new_telegram_session(auth, topic_id)
        if prior is not None:
            append_recovery_log(
                session_store.paths.recovery_log,
                f"session_detached_on_new {session_log_label(prior)} replacement_session_id={session.session_id}",
            )
        append_recovery_log(
            session_store.paths.recovery_log,
            f"session_attached_on_new {session_log_label(session)}",
        )
        send_telegram_message(
            telegram,
            auth.telegram_chat_id,
            f"Started new session {session.session_id}.",
            performance=performance,
            category="status",
            session_id=session.session_id,
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
                performance=performance,
                category="status",
            )
            return
        if not hasattr(codex, "interrupt"):
            send_telegram_message(
                telegram,
                auth.telegram_chat_id,
                "Stop is not supported by the current Codex runtime.",
                performance=performance,
                category="status",
            )
            return
        try:
            stopped = codex.interrupt(topic_id=topic_id)
        except TypeError:
            stopped = codex.interrupt()
        if stopped:
            send_telegram_message(
                telegram,
                auth.telegram_chat_id,
                "Stopped the active turn.",
                performance=performance,
                category="status",
            )
        else:
            send_telegram_message(
                telegram,
                auth.telegram_chat_id,
                "No active turn to stop.",
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
                performance=performance,
                category="status",
            )
            return
        if not hasattr(codex, "interrupt"):
            send_telegram_message(
                telegram,
                auth.telegram_chat_id,
                "Abort is not supported by the current Codex runtime.",
                performance=performance,
                category="status",
            )
            return
        try:
            stopped = codex.interrupt(topic_id=topic_id)
        except TypeError:
            stopped = codex.interrupt()
        if stopped:
            send_telegram_message(
                telegram,
                auth.telegram_chat_id,
                "Aborted the active turn.",
                performance=performance,
                category="status",
            )
        else:
            send_telegram_message(
                telegram,
                auth.telegram_chat_id,
                "No active turn to abort.",
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
                performance=performance,
                category="status",
            )
            return
    if codex is None:
        send_telegram_message(
            telegram,
            auth.telegram_chat_id,
            "Codex is not ready yet.",
            performance=performance,
            category="status",
        )
        return
    session_id: str | None = None
    if session_store is not None and performance is not None:
        tracked_session = session_store.get_or_create_telegram_session(auth, topic_id)
        session_id = tracked_session.session_id
        performance.mark_turn_requested(tracked_session, topic_id=topic_id, text=text)
    try:
        codex.send(text, topic_id=topic_id)
    except TypeError:
        try:
            codex.send(text)
        except Exception as exc:
            if performance is not None and session_id is not None:
                performance.mark_turn_failed(session_id, error=str(exc))
            send_telegram_message(
                telegram,
                auth.telegram_chat_id,
                f"Codex request failed: {exc}",
                performance=performance,
                category="error",
            )
            return
    except Exception as exc:
        if performance is not None and session_id is not None:
            performance.mark_turn_failed(session_id, error=str(exc))
        send_telegram_message(
            telegram,
            auth.telegram_chat_id,
            f"Codex request failed: {exc}",
            performance=performance,
            category="error",
        )
        return
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
    if isinstance(update_id, int):
        update_store = TelegramUpdateStore(paths)
        if not update_store.mark_processed(update_id):
            return codex

    session_store = SessionStore(paths)
    topic_id = extract_update_topic_id(update)
    message = update.get("message", {}) or {}
    chat_id = message.get("chat", {}).get("id")
    text = (message.get("text") or "").strip()
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
        if chat_id:
            send_telegram_message(
                telegram,
                chat_id,
                "This bot is already paired to another chat.",
                performance=performance,
                category="pairing",
            )
        return codex
    if status == "code-issued":
        if auth.pending_chat_id and auth.pairing_code:
            send_telegram_message(
                telegram,
                auth.pending_chat_id,
                f"Pairing code: {auth.pairing_code}. Enter this code in the local Tele Cli terminal to authorize this chat.",
                performance=performance,
                category="pairing",
            )
        print(
            "Pairing requested. "
            f"chat_id={auth.pending_chat_id} user_id={auth.pending_user_id} code={auth.pairing_code}"
        )
        if isatty():
            if complete_pending_pairing(paths, auth, telegram, allow_empty=True) and codex is None:
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

    if text == "/model":
        send_telegram_message(
            telegram,
            auth.telegram_chat_id,
            'Usage: /model <name>',
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
            auth.telegram_chat_id,
            restart_status_text(model_value, "Model", codex),
            performance=performance,
            category="status",
        )
        return codex

    if text == "/reasoning":
        send_telegram_message(
            telegram,
            auth.telegram_chat_id,
            'Usage: /reasoning <minimal|low|medium|high|xhigh>',
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
                auth.telegram_chat_id,
                "Reasoning must be one of: minimal, low, medium, high, xhigh.",
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
            auth.telegram_chat_id,
            restart_status_text(normalized_reasoning, "Reasoning", codex),
            performance=performance,
            category="status",
        )
        return codex

    if text in {"/status", "/sessions", "/new", "/stop", "/abort"} or text.startswith("/approve ") or text.startswith("/deny "):
        handle_authorized_message(
            text,
            auth,
            runtime_state,
            codex,
            telegram,
            recorder,
            session_store,
            topic_id,
            performance,
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
            auth,
            runtime_state,
            codex,
            telegram,
            recorder,
            session_store,
            topic_id,
            performance,
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
    performance: PerformanceTracker | None = None,
) -> None:
    if codex is None or not hasattr(codex, "poll_notification"):
        return
    session_store = SessionStore(paths)
    while True:
        notification = codex.poll_notification()
        if notification is None:
            break
        method = notification.method
        params = notification.params or {}
        append_app_server_notification_log(paths, method, params)
        if performance is not None:
            performance.mark_notification_received(method, params)
        session = resolve_notification_session(session_store, auth, params)
        thinking_delta = extract_thinking_delta(method, params)
        if session is not None and thinking_delta is not None:
            append_thinking_delta(auth, telegram, session, thinking_delta, performance=performance)
            session_store.save_session(session)
            continue
        if method in {
            "assistant/message.delta",
            "item/agentMessage/delta",
            "item/updated",
            "item/started",
            "item/completed",
            "turn/output",
        }:
            text = extract_assistant_text(params)
            thinking_text = extract_thinking_text(params)
            activity_text = extract_activity_text(method, params)
            if session is not None and text:
                if performance is not None:
                    performance.mark_reply_started(session, trigger=method)
                session_store.append_pending_output(session, text)
                if method == "item/agentMessage/delta":
                    maybe_stream_partial_output(
                        auth,
                        telegram,
                        recorder,
                        session_store,
                        session,
                        performance=performance,
                    )
            elif session is not None and thinking_text:
                ensure_thinking_message(auth, telegram, session, text=thinking_text, performance=performance)
                session_store.save_session(session)
            elif session is not None and activity_text:
                ensure_thinking_message(auth, telegram, session, text=activity_text, performance=performance)
                session_store.save_session(session)
            continue
        if session is not None:
            status_text = extract_event_driven_status(method, params)
            if status_text:
                ensure_thinking_message(auth, telegram, session, text=status_text, performance=performance)
                session_store.save_session(session)
        if method in {"account/updated", "account/ready", "login/completed"}:
            account_payload = extract_account_payload(params)
            if account_payload is None:
                continue
            next_state = update_codex_auth_state(
                paths,
                account_payload=account_payload,
                runtime=runtime,
                runtime_state=runtime_state,
            )
            if auth.telegram_chat_id and next_state == "RUNNING":
                send_telegram_message(
                    telegram,
                    auth.telegram_chat_id,
                    "Codex login completed. Telegram and Codex are ready.",
                    performance=performance,
                    category="startup_notification",
                )
            continue
        if method == "assistant/message.partial":
            text = extract_assistant_text(params)
            session = resolve_notification_session(session_store, auth, params)
            if session is not None:
                if text:
                    if performance is not None:
                        performance.mark_reply_started(session, trigger=method)
                    session_store.append_pending_output(session, text)
                flush_buffer(
                    session.session_id,
                    auth,
                    telegram,
                    recorder,
                    session_store,
                    mark_agent=False,
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
            assistant_text = extract_assistant_text(params)
            if not assistant_text and not session.pending_output_text.strip() and session.thread_id and hasattr(codex, "read_thread"):
                try:
                    assistant_text = extract_latest_agent_message(codex.read_thread(session.thread_id, include_turns=True))
                except Exception:
                    assistant_text = None
            if assistant_text:
                if performance is not None:
                    performance.mark_reply_started(session, trigger=method)
                session_store.append_pending_output(session, assistant_text)
            session.active_turn_id = None
            session.last_completed_turn_id = str(turn_id)
            session.status = "ACTIVE"
            session_store.save_session(session)
            if performance is not None:
                performance.mark_reply_finished(
                    session,
                    outcome="completed" if method == "turn/completed" else "failed",
                )
            flush_buffer(
                session.session_id,
                auth,
                telegram,
                recorder,
                session_store,
                mark_agent=True,
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
    recorder = Recorder(paths.terminal_log)
    performance = PerformanceTracker(paths.performance_log)
    debug = DebugMirror()
    telegram = TelegramClient(auth.bot_token)
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
        save_json(paths.runtime, runtime_state.to_dict())
        if auth.telegram_chat_id:
            send_telegram_message(
                telegram,
                auth.telegram_chat_id,
                f"[{source}] {line[:3500]}",
                performance=performance,
                category="debug_output",
            )

    codex = None
    last_typing_sent_at: datetime | None = None
    codex_restart_failures = 0
    next_codex_restart_at = 0.0
    updates_queue: queue.Queue[dict] = queue.Queue()
    stop_event = threading.Event()
    poll_gate = threading.Event()
    poll_gate.set()
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
    save_json(paths.runtime, runtime_state.to_dict())
    append_recovery_log(paths.recovery_log, f"service started session_id={runtime_state.session_id}")
    if stale_approvals and auth.telegram_chat_id:
        send_telegram_message(
            telegram,
            auth.telegram_chat_id,
            f"{stale_approvals} pending approval request(s) were marked stale after restart.",
            performance=performance,
            category="approval",
        )
    telegram_thread = start_telegram_polling_thread(
        paths=paths,
        config=config,
        telegram=telegram,
        runtime_state=runtime_state,
        update_queue=updates_queue,
        stop_event=stop_event,
        poll_gate=poll_gate,
    )
    threading.Event().wait(0.02)

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
                drain_codex_approvals(paths, auth, telegram, codex, performance)
                drain_codex_notifications(paths, auth, telegram, recorder, codex, runtime, runtime_state, performance)
                flush_idle_partial_outputs(
                    paths,
                    auth,
                    telegram,
                    recorder,
                    session_store,
                    idle_seconds=config.partial_flush_idle_seconds,
                    performance=performance,
                )
                last_typing_sent_at = maybe_send_typing_indicator(
                    paths,
                    auth,
                    telegram,
                    session_store,
                    interval_seconds=config.typing_indicator_interval_seconds,
                    last_sent_at=last_typing_sent_at,
                )
                maybe_refresh_thinking_message(
                    paths,
                    auth,
                    telegram,
                    session_store,
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
            if processed_updates:
                poll_gate.set()
                threading.Event().wait(0.02)
                continue
            drain_codex_approvals(paths, auth, telegram, codex, performance)
            drain_codex_notifications(paths, auth, telegram, recorder, codex, runtime, runtime_state, performance)
            flush_idle_partial_outputs(
                paths,
                auth,
                telegram,
                recorder,
                session_store,
                idle_seconds=config.partial_flush_idle_seconds,
                performance=performance,
            )
            last_typing_sent_at = maybe_send_typing_indicator(
                paths,
                auth,
                telegram,
                session_store,
                interval_seconds=config.typing_indicator_interval_seconds,
                last_sent_at=last_typing_sent_at,
            )
            maybe_refresh_thinking_message(
                paths,
                auth,
                telegram,
                session_store,
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
            poll_gate.set()
            time.sleep(service_tick_seconds(config))
    finally:
        threading.Event().wait(0.05)
        stop_event.set()
        poll_gate.set()
        telegram_thread.join(timeout=0.5)
        if codex is not None:
            codex.stop()
            runtime.stop_codex()
        recorder.stop()
        debug.stop()
        app_lock.clear()
        append_recovery_log(paths.recovery_log, "service stopped")
