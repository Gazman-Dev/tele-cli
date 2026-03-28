from __future__ import annotations

from datetime import datetime, timezone
import inspect
import json
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
from .instructions import ensure_instruction_files
from .performance import PerformanceTracker, edit_telegram_message, send_telegram_message
from .recorder import Recorder
from .runtime import ServiceRuntime
from .session_store import SessionStore
from .sleep import has_pending_sleep_work, run_sleep, should_run_sleep
from .telegram_markdown import (
    code_block_telegram_markdown_v2,
    escape_telegram_markdown_v2,
    normalize_existing_telegram_markdown_v2,
    normalize_telegram_markdown_source,
    safe_stream_markdown_v2,
    to_telegram_markdown_v2,
)
from .telegram_update_store import TelegramUpdateStore


LOCAL_AUTH_CALLBACK_RE = re.compile(r"https?://(?:localhost|127\.0\.0\.1):1455/auth/callback\?[^\s]+", re.IGNORECASE)
TELEGRAM_TEXT_LIMIT = 4000
TELEGRAM_MARKDOWN_MODE = "MarkdownV2"
_AGENT_MESSAGE_PHASES: dict[str, str] = {}
_AGENT_MESSAGE_TEXTS: dict[str, str] = {}
COMMENTARY_STREAM_MIN_INTERVAL_SECONDS = 1.0
DEFAULT_THINKING_STREAM_MIN_INTERVAL_SECONDS = 1.0


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
        "assistant_text": extract_assistant_text(params),
        "thinking_text": extract_thinking_text(params),
        "activity_text": extract_activity_text(method, params),
        "status_text": extract_event_driven_status(method, params),
        "params": params,
    }
    with paths.root.joinpath("app_server_notifications.log").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def append_telegram_format_failure_log(
    paths: AppPaths,
    *,
    session_id: str,
    thread_id: str | None,
    turn_id: str | None,
    stage: str,
    error: str,
    raw_text: str,
    rich_text: str,
    escaped_text: str,
    emergency_text: str | None = None,
) -> None:
    paths.root.mkdir(parents=True, exist_ok=True)
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
    with paths.root.joinpath("telegram_format_failures.log").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def append_telegram_poll_log(paths: AppPaths, event: str, **fields: object) -> None:
    paths.root.mkdir(parents=True, exist_ok=True)
    record = {"timestamp": utc_now(), "event": event}
    record.update(fields)
    with paths.root.joinpath("telegram_poll.log").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


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
                append_telegram_poll_log(paths, "poll_error", error=str(exc), backoff_seconds=delay)
                continue
            except Exception as exc:
                telegram_failures += 1
                delay = telegram_retry_delay(config, telegram_failures)
                next_poll_at = time.monotonic() + delay
                runtime_state.telegram_state = "BACKOFF"
                save_json(paths.runtime, runtime_state.to_dict())
                append_recovery_log(paths.recovery_log, f"telegram poll crashed -> backoff={delay:.1f}s error={exc}")
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
) -> str:
    metadata = telegram.get_file(file_id)
    file_path = str(metadata.get("file_path") or "").strip()
    if not file_path:
        raise TelegramError(f"Telegram file metadata missing file_path for {file_id}")
    destination = telegram_media_dir(paths) / f"{utc_now().replace(':', '').replace('-', '')}_{sanitize_telegram_filename(suggested_name)}"
    destination.write_bytes(telegram.download_file(file_path))
    return destination.relative_to(paths.root).as_posix()


def build_telegram_attachment_notes(paths: AppPaths, telegram: TelegramClient, message: dict) -> list[str]:
    notes: list[str] = []
    document = message.get("document")
    if isinstance(document, dict) and document.get("file_id"):
        file_name = str(document.get("file_name") or f"document_{document.get('file_unique_id') or document['file_id']}")
        relative_path = save_telegram_file(paths, telegram, file_id=str(document["file_id"]), suggested_name=file_name)
        mime_type = document.get("mime_type") or mimetypes.guess_type(file_name)[0] or "application/octet-stream"
        notes.append(f"File saved to {relative_path} (name={file_name}, mime={mime_type}).")
    photos = message.get("photo")
    if isinstance(photos, list) and photos:
        photo = photos[-1]
        if isinstance(photo, dict) and photo.get("file_id"):
            photo_name = f"photo_{photo.get('file_unique_id') or photo['file_id']}.jpg"
            relative_path = save_telegram_file(paths, telegram, file_id=str(photo["file_id"]), suggested_name=photo_name)
            notes.append(f"Image saved to {relative_path}.")
    return notes


def build_telegram_input_text(paths: AppPaths, telegram: TelegramClient, message: dict) -> str:
    base_text = str(message.get("text") or message.get("caption") or "").strip()
    attachment_notes = build_telegram_attachment_notes(paths, telegram, message)
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


def _extract_search_hint(arguments: object) -> str | None:
    if not isinstance(arguments, dict):
        return None
    for key in ("query", "q", "searchTerm", "term", "prompt"):
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            return _shorten_activity_text(value.strip(), limit=60)
    return None


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


def extract_activity_text(method: str, params: dict) -> str | None:
    if method == "item/commandExecution/outputDelta":
        delta = _extract_delta_text(params)
        if delta:
            return f"Command output: {delta}"
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
    if method == "serverRequest/resolved":
        return "Approval resolved."
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
        stream_format=True,
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
    body = (text or "").strip()
    if not body:
        return "Thinking"
    if body == "Thinking":
        return body
    return f"Thinking\n\n{body}"


def extract_thinking_body(text: str | None) -> str:
    value = (text or "").strip()
    if value.startswith("Thinking\n\n"):
        return value[len("Thinking\n\n") :].strip()
    if value == "Thinking":
        return ""
    return value


def set_visible_thinking_message(
    auth: AuthState,
    telegram: TelegramClient,
    recorder: Recorder,
    session_store: SessionStore,
    session,
    *,
    text: str | None = None,
    performance: PerformanceTracker | None = None,
    min_interval_seconds: float = DEFAULT_THINKING_STREAM_MIN_INTERVAL_SECONDS,
) -> None:
    ensure_thinking_message(auth, telegram, session, text=text, performance=performance)
    session.streaming_phase = "commentary"
    replace_pending_output(session_store, session, render_thinking_message(session.thinking_message_text))
    maybe_stream_partial_output(
        auth,
        telegram,
        recorder,
        session_store,
        session,
        performance=performance,
        min_interval_seconds=min_interval_seconds,
    )


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
        if session.pending_output_text.strip() or session.streaming_output_text.strip():
            continue
        set_visible_thinking_message(
            auth,
            telegram,
            recorder,
            session_store,
            session,
            text=session.thinking_message_text or default_thinking_text(session),
            performance=performance,
        )


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
    existing_text = session.thinking_message_text or extract_thinking_body(session.streaming_output_text)
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
    stream_format: bool = False,
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
            session.streaming_phase = ""
            session.thinking_message_text = ""
            session_store.save_session(session)
        session_store.consume_pending_output(session)
        return
    plain_chunks = split_telegram_text(text)
    if not plain_chunks:
        return
    context = {
        "performance": performance,
        "category": "assistant_output",
        "session_id": session.session_id,
        "thread_id": session.thread_id,
        "turn_id": session.active_turn_id or session.last_completed_turn_id,
    }
    parse_mode = TELEGRAM_MARKDOWN_MODE if (mark_agent or stream_format) else None
    def _deliver(chunks: list[str], *, parse_mode_value: str | None) -> None:
        if session.streaming_message_id is not None:
            edit_telegram_message(
                telegram,
                target_chat_id,
                session.streaming_message_id,
                chunks[0],
                parse_mode=parse_mode_value,
                **context,
            )
        else:
            message_id = send_telegram_message(
                telegram,
                target_chat_id,
                chunks[0],
                topic_id=session.transport_topic_id,
                parse_mode=parse_mode_value,
                **context,
            )
            if not mark_agent:
                session.streaming_message_id = message_id
                session_store.save_session(session)
        for chunk in chunks[1:]:
            send_telegram_message(
                telegram,
                target_chat_id,
                chunk,
                topic_id=session.transport_topic_id,
                parse_mode=parse_mode_value,
                **context,
            )

    if mark_agent:
        raw_chunks = [normalize_existing_telegram_markdown_v2(chunk) for chunk in plain_chunks]
        normalized_source_chunks = [normalize_telegram_markdown_source(chunk) for chunk in plain_chunks]
        formatted_chunks = [to_telegram_markdown_v2(chunk) for chunk in normalized_source_chunks]
        escaped_chunks = [escape_telegram_markdown_v2(chunk) for chunk in normalized_source_chunks]
        emergency_chunks = [code_block_telegram_markdown_v2(chunk) for chunk in normalized_source_chunks]
        attempts = [
            ("raw_markdown", raw_chunks),
            ("formatted_markdown", formatted_chunks),
            ("escaped_markdown", escaped_chunks),
            ("code_block_markdown", emergency_chunks),
        ]
        rich_text = "\n\n---chunk---\n\n".join(formatted_chunks)
        escaped_text = "\n\n---chunk---\n\n".join(escaped_chunks)
        emergency_text = "\n\n---chunk---\n\n".join(emergency_chunks)
        delivered = False
        for stage, attempt_chunks in attempts:
            try:
                _deliver(attempt_chunks, parse_mode_value=TELEGRAM_MARKDOWN_MODE)
                delivered = True
                break
            except TelegramError as exc:
                append_telegram_format_failure_log(
                    session_store.paths,
                    session_id=session.session_id,
                    thread_id=session.thread_id,
                    turn_id=session.active_turn_id or session.last_completed_turn_id,
                    stage=stage,
                    error=str(exc),
                    raw_text=text,
                    rich_text=rich_text,
                    escaped_text=escaped_text,
                    emergency_text=emergency_text,
                )
        if not delivered:
            raise TelegramError("All Telegram MarkdownV2 delivery attempts failed.")
        session.streaming_message_id = None
        session_store.save_session(session)
    else:
        stream_chunks = [safe_stream_markdown_v2(chunk) for chunk in plain_chunks] if stream_format else plain_chunks
        try:
            _deliver(stream_chunks, parse_mode_value=parse_mode)
        except TelegramError:
            if session.streaming_message_id is not None:
                try:
                    edit_telegram_message(
                        telegram,
                        target_chat_id,
                        session.streaming_message_id,
                        "Reply continues below.",
                        **context,
                    )
                except TelegramError:
                    pass
            for chunk in plain_chunks:
                send_telegram_message(
                    telegram,
                    target_chat_id,
                    chunk,
                    topic_id=session.transport_topic_id,
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
        session.streaming_phase = ""
        session.thinking_message_text = ""
        session_store.save_session(session)
    session_store.consume_pending_output(session)
    pruned = session_store.prune_detached_sessions()
    if pruned:
        append_recovery_log(session_store.paths.recovery_log, f"detached_sessions_pruned count={pruned}")


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
    if interval_seconds <= 0:
        return last_sent_at
    if ApprovalStore(paths).pending():
        return last_sent_at
    current = get_latest_user_session(session_store, auth, require_active_turn=True)
    if current is None or not current.attached or not current.active_turn_id:
        return last_sent_at
    target_chat_id = current.transport_chat_id or auth.telegram_chat_id
    if not target_chat_id:
        return last_sent_at
    now = now or datetime.now(timezone.utc)
    effective_interval = min(interval_seconds, 2.5)
    if last_sent_at is not None and (now - last_sent_at).total_seconds() < effective_interval:
        return last_sent_at
    if hasattr(telegram, "send_typing"):
        try:
            telegram.send_typing(target_chat_id, topic_id=current.transport_topic_id)
        except TypeError:
            telegram.send_typing(target_chat_id)
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
    paths: AppPaths | None = None,
    config: Config | None = None,
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
                topic_id=topic_id,
                performance=performance,
                category="status",
            )
        else:
            send_telegram_message(
                telegram,
                auth.telegram_chat_id,
                f"Codex login callback failed: {detail}",
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
        session = session_store.create_new_telegram_session(auth, topic_id)
        session.attached = True
        session.thread_id = None
        session.active_turn_id = None
        session.pending_output_text = ""
        session.pending_output_updated_at = None
        session.last_completed_turn_id = None
        session.last_delivered_output_text = ""
        session.streaming_message_id = None
        session.streaming_output_text = ""
        session.streaming_phase = ""
        session.thinking_message_text = ""
        session.status = "ACTIVE"
        session_store.save_session(session)
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
    session_id: str | None = None
    recovered_from_stale_turn = False
    if session_store is not None and performance is not None:
        tracked_session = session_store.get_or_create_telegram_session(auth, topic_id)
        session_id = tracked_session.session_id
        performance.mark_turn_requested(tracked_session, topic_id=topic_id, text=text)
    try:
        send_result = codex.send(text, topic_id=topic_id, chat_id=auth.telegram_chat_id, user_id=auth.telegram_user_id)
        recovered_from_stale_turn = bool(send_result)
    except TypeError:
        try:
            send_result = codex.send(text, topic_id=topic_id)
            recovered_from_stale_turn = bool(send_result)
        except TypeError:
            try:
                send_result = codex.send(text)
                recovered_from_stale_turn = bool(send_result)
            except Exception as exc:
                if performance is not None and session_id is not None:
                    performance.mark_turn_failed(session_id, error=str(exc))
                send_telegram_message(
                    telegram,
                    auth.telegram_chat_id,
                    f"Codex request failed: {exc}",
                    topic_id=topic_id,
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
                topic_id=topic_id,
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
            topic_id=topic_id,
            performance=performance,
            category="error",
        )
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
    if isinstance(update_id, int):
        update_store = TelegramUpdateStore(paths)
        if not update_store.mark_processed(update_id):
            return codex

    session_store = SessionStore(paths)
    topic_id = extract_update_topic_id(update)
    message = update.get("message", {}) or {}
    chat_id = message.get("chat", {}).get("id")
    user_id = message.get("from", {}).get("id")
    text = build_telegram_input_text(paths, telegram, message)
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
                topic_id=topic_id,
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
    max_notifications: int | None = None,
) -> int:
    if codex is None or not hasattr(codex, "poll_notification"):
        return 0
    session_store = SessionStore(paths)
    handled = 0
    while True:
        if max_notifications is not None and handled >= max_notifications:
            break
        notification = codex.poll_notification()
        if notification is None:
            break
        handled += 1
        method = notification.method
        params = notification.params or {}
        agent_message_phase = remember_agent_message_phase(method, params)
        append_app_server_notification_log(paths, method, params)
        if performance is not None:
            performance.mark_notification_received(method, params)
        session = resolve_notification_session(session_store, auth, params)
        thinking_delta = extract_thinking_delta(method, params)
        if session is not None and thinking_delta is not None:
            append_thinking_delta(auth, telegram, session, thinking_delta, performance=performance)
            session_store.save_session(session)
            set_visible_thinking_message(
                auth,
                telegram,
                recorder,
                session_store,
                session,
                text=session.thinking_message_text,
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
            if session is not None and commentary_text:
                if performance is not None:
                    performance.mark_reply_started(session, trigger=method)
                set_visible_thinking_message(
                    auth,
                    telegram,
                    recorder,
                    session_store,
                    session,
                    text=commentary_text,
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
                    if performance is not None:
                        performance.mark_reply_started(session, trigger=method)
                    if action == "replace":
                        replace_pending_output(session_store, session, payload)
                        session.streaming_output_text = ""
                        session.streaming_phase = "answer"
                        session_store.save_session(session)
                    else:
                        session_store.append_pending_output(session, payload)
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
                set_visible_thinking_message(
                    auth,
                    telegram,
                    recorder,
                    session_store,
                    session,
                    text=thinking_text,
                    performance=performance,
                )
            elif session is not None and activity_text:
                set_visible_thinking_message(
                    auth,
                    telegram,
                    recorder,
                    session_store,
                    session,
                    text=activity_text,
                    performance=performance,
                )
            continue
        if session is not None:
            status_text = extract_event_driven_status(method, params)
            if status_text:
                set_visible_thinking_message(
                    auth,
                    telegram,
                    recorder,
                    session_store,
                    session,
                    text=status_text,
                    performance=performance,
                )
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
                    stream_format=True,
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
            if should_append_completion_text(session, assistant_text):
                action, payload = merge_incremental_assistant_text(session, assistant_text)
                if action != "ignore" and payload:
                    if performance is not None:
                        performance.mark_reply_started(session, trigger=method)
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
            if performance is not None:
                performance.mark_reply_finished(
                    session,
                    outcome="completed" if method == "turn/completed" else "failed",
                )
            if not session.pending_output_text.strip():
                session.streaming_message_id = None
                session.streaming_output_text = ""
                session.streaming_phase = ""
                session.thinking_message_text = ""
                session_store.save_session(session)
                continue
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

    codex = None
    last_typing_sent_at: datetime | None = None
    codex_restart_failures = 0
    next_codex_restart_at = 0.0
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
    save_json(paths.runtime, runtime_state.to_dict())
    append_recovery_log(paths.recovery_log, f"service started session_id={runtime_state.session_id}")
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
    startup_now = datetime.now().astimezone()
    if has_pending_sleep_work(paths) and should_run_sleep(paths, startup_now, config.sleep_hour_local):
        poll_gate.set()
        try:
            run_sleep(paths, config, startup_now, config.sleep_hour_local)
        except Exception as exc:
            append_recovery_log(paths.recovery_log, f"sleep failed on startup -> {exc}")

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
                append_recovery_log(paths.recovery_log, "telegram poll thread stopped -> restarting")
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
                drain_codex_approvals(paths, auth, telegram, codex, performance)
                drain_codex_notifications(
                    paths,
                    auth,
                    telegram,
                    recorder,
                    codex,
                    runtime,
                    runtime_state,
                    performance,
                    max_notifications=100,
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
                last_typing_sent_at = maybe_send_typing_indicator(
                    paths,
                    auth,
                    telegram,
                    session_store,
                    interval_seconds=config.typing_indicator_interval_seconds,
                    last_sent_at=last_typing_sent_at,
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
            if processed_updates:
                poll_gate.set()
                threading.Event().wait(0.02)
                continue
            if time.monotonic() - last_sleep_check >= 30.0:
                last_sleep_check = time.monotonic()
                current_local = datetime.now().astimezone()
                if has_pending_sleep_work(paths) and should_run_sleep(paths, current_local, config.sleep_hour_local):
                    poll_gate.set()
                    try:
                        run_sleep(paths, config, current_local, config.sleep_hour_local)
                    except Exception as exc:
                        append_recovery_log(paths.recovery_log, f"sleep failed during service loop -> {exc}")
            drain_codex_approvals(paths, auth, telegram, codex, performance)
            drain_codex_notifications(
                paths,
                auth,
                telegram,
                recorder,
                codex,
                runtime,
                runtime_state,
                performance,
                max_notifications=100,
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
            last_typing_sent_at = maybe_send_typing_indicator(
                paths,
                auth,
                telegram,
                session_store,
                interval_seconds=config.typing_indicator_interval_seconds,
                last_sent_at=last_typing_sent_at,
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
