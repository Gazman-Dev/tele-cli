from __future__ import annotations

from datetime import datetime, timezone
import time
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
from .control import ServiceConflictChoices, isatty, prepare_service_lock, reset_auth, start_codex_session
from .recorder import Recorder
from .runtime import ServiceRuntime
from .session_store import SessionStore
from .telegram_update_store import TelegramUpdateStore


LOCAL_AUTH_CALLBACK_RE = re.compile(r"https?://(?:localhost|127\.0\.0\.1):1455/auth/callback\?[^\s]+", re.IGNORECASE)


def build_status_message(
    auth: AuthState,
    runtime_state: RuntimeState,
    session_store: SessionStore | None = None,
    topic_id: int | None = None,
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


def extract_update_topic_id(update: dict) -> int | None:
    message = update.get("message") or {}
    topic_id = message.get("message_thread_id")
    return int(topic_id) if isinstance(topic_id, int) else None


def extract_assistant_text(params: dict) -> str | None:
    candidates = [
        params.get("outputText"),
        params.get("text"),
        params.get("finalText"),
        (params.get("result") or {}).get("outputText") if isinstance(params.get("result"), dict) else None,
        (params.get("result") or {}).get("text") if isinstance(params.get("result"), dict) else None,
    ]
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return candidate
    turn = params.get("turn")
    if isinstance(turn, dict):
        for item in turn.get("items") or []:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "agentMessage":
                text = item.get("text")
                if isinstance(text, str) and text.strip():
                    return text
    return None


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
) -> None:
    session = next((item for item in session_store.load().sessions if item.session_id == session_id), None)
    if session is None:
        return
    text = session.pending_output_text.strip()
    if not text:
        return
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
        session_store.consume_pending_output(session)
        return
    telegram.send_message(auth.telegram_chat_id, text)
    recorder.record("assistant", text)
    session_store.mark_delivered_output(session, text)
    if mark_agent:
        session_store.mark_agent_message(session)
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
        flush_buffer(session.session_id, auth, telegram, recorder, session_store, mark_agent=False)


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
            telegram.send_message(
                auth.telegram_chat_id,
                f"Codex App Server stopped. Restarting in {delay:.1f}s. Telegram remains available.",
            )
    if codex is None and is_auth_paired(auth) and now >= next_restart_at:
        restarted = start_codex_session_fn(
            config,
            auth,
            runtime,
            runtime_state,
            metadata,
            app_lock,
            telegram,
            handle_output,
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
) -> None:
    if not auth.telegram_chat_id:
        return
    callback_url = extract_login_callback_url(text)
    if runtime_state.codex_state == "AUTH_REQUIRED" and callback_url:
        ok, detail = replay_login_callback(callback_url)
        if ok:
            telegram.send_message(auth.telegram_chat_id, "Codex login callback received. Waiting for Codex to finish sign-in.")
        else:
            telegram.send_message(auth.telegram_chat_id, f"Codex login callback failed: {detail}")
        return
    if text == "/status":
        telegram.send_message(auth.telegram_chat_id, build_status_message(auth, runtime_state, session_store, topic_id))
        return
    if text == "/sessions":
        sessions = session_store.list_telegram_sessions(auth, topic_id) if session_store is not None else []
        if not sessions:
            telegram.send_message(auth.telegram_chat_id, "No sessions yet.")
            return
        lines = ["Sessions"]
        for session in sessions:
            lines.append(f"{session.session_id} status={session.status} thread={session.thread_id or 'none'}")
        telegram.send_message(auth.telegram_chat_id, "\n".join(lines))
        return
    if text == "/new":
        if session_store is None:
            telegram.send_message(auth.telegram_chat_id, "Session store is not available.")
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
        telegram.send_message(auth.telegram_chat_id, f"Started new session {session.session_id}.")
        return
    approval_store = ApprovalStore(session_store.paths) if session_store is not None else None
    approve_id = parse_request_command(text, "/approve")
    if approve_id is not None:
        if codex is None or not hasattr(codex, "approve") or approval_store is None:
            telegram.send_message(auth.telegram_chat_id, "Approval handling is not available.")
            return
        approval = approval_store.get_pending(approve_id)
        if approval is None:
            telegram.send_message(auth.telegram_chat_id, f"No pending approval {approve_id}.")
            return
        codex.approve(approve_id)
        approval_store.mark(approve_id, "approved")
        telegram.send_message(auth.telegram_chat_id, f"Approved request {approve_id}.")
        return
    deny_id = parse_request_command(text, "/deny")
    if deny_id is not None:
        if codex is None or not hasattr(codex, "deny") or approval_store is None:
            telegram.send_message(auth.telegram_chat_id, "Approval handling is not available.")
            return
        approval = approval_store.get_pending(deny_id)
        if approval is None:
            telegram.send_message(auth.telegram_chat_id, f"No pending approval {deny_id}.")
            return
        codex.deny(deny_id)
        approval_store.mark(deny_id, "denied")
        telegram.send_message(auth.telegram_chat_id, f"Denied request {deny_id}.")
        return
    if text == "/stop":
        if codex is None:
            telegram.send_message(auth.telegram_chat_id, "No active turn to stop.")
            return
        if not hasattr(codex, "interrupt"):
            telegram.send_message(auth.telegram_chat_id, "Stop is not supported by the current Codex runtime.")
            return
        try:
            stopped = codex.interrupt(topic_id=topic_id)
        except TypeError:
            stopped = codex.interrupt()
        if stopped:
            telegram.send_message(auth.telegram_chat_id, "Stopped the active turn.")
        else:
            telegram.send_message(auth.telegram_chat_id, "No active turn to stop.")
        return
    if session_store is not None:
        current = session_store.get_current_telegram_session(auth, topic_id)
        if current is not None and current.status == "RECOVERING_TURN":
            telegram.send_message(
                auth.telegram_chat_id,
                "Current session is recovering an in-flight turn. Wait for recovery, use /stop, or start fresh with /new.",
            )
            return
    if codex is None:
        telegram.send_message(auth.telegram_chat_id, "Codex is not ready yet.")
        return
    try:
        codex.send(text, topic_id=topic_id)
    except TypeError:
        try:
            codex.send(text)
        except Exception as exc:
            telegram.send_message(auth.telegram_chat_id, f"Codex request failed: {exc}")
            return
    except Exception as exc:
        telegram.send_message(auth.telegram_chat_id, f"Codex request failed: {exc}")
        return
    recorder.record("telegram", text)


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
):
    update_id = update.get("update_id")
    if isinstance(update_id, int):
        update_store = TelegramUpdateStore(paths)
        if not update_store.mark_processed(update_id):
            return codex

    session_store = SessionStore(paths)
    topic_id = extract_update_topic_id(update)
    ok, status = register_pairing_request(auth, update)
    save_json(paths.auth, auth.to_dict())
    if status == "already-paired":
        chat_id = update.get("message", {}).get("chat", {}).get("id")
        if chat_id:
            telegram.send_message(chat_id, "This bot is already paired to another chat.")
        return codex
    if status == "code-issued":
        if auth.pending_chat_id and auth.pairing_code:
            telegram.send_message(
                auth.pending_chat_id,
                f"Pairing code: {auth.pairing_code}. Enter this code in the local Tele Cli terminal to authorize this chat.",
            )
        print(
            "Pairing requested. "
            f"chat_id={auth.pending_chat_id} user_id={auth.pending_user_id} code={auth.pairing_code}"
        )
        if isatty():
            if complete_pending_pairing(paths, auth, telegram, allow_empty=True) and codex is None:
                codex = start_codex_session_fn(
                    config,
                    auth,
                    runtime,
                    runtime_state,
                    metadata,
                    app_lock,
                    telegram,
                    handle_output,
                )
        return codex
    if not ok:
        return codex

    text = (update.get("message", {}).get("text") or "").strip()
    if text in {"/status", "/sessions", "/new", "/stop"} or text.startswith("/approve ") or text.startswith("/deny "):
        handle_authorized_message(text, auth, runtime_state, codex, telegram, recorder, session_store, topic_id)
        return codex

    if codex is None and is_auth_paired(auth):
        codex = start_codex_session_fn(
            config,
            auth,
            runtime,
            runtime_state,
            metadata,
            app_lock,
            telegram,
            handle_output,
        )

    if text:
        handle_authorized_message(text, auth, runtime_state, codex, telegram, recorder, session_store, topic_id)
    return codex


def drain_codex_approvals(paths: AppPaths, auth: AuthState, telegram: TelegramClient, codex) -> None:
    if codex is None or not hasattr(codex, "poll_approval_request") or not auth.telegram_chat_id:
        return
    approval_store = ApprovalStore(paths)
    while True:
        approval = codex.poll_approval_request()
        if approval is None:
            break
        approval_store.add(approval)
        telegram.send_message(
            auth.telegram_chat_id,
            f"Approval needed {approval.request_id}: {approval.method}. Reply with /approve {approval.request_id} or /deny {approval.request_id}.",
        )


def drain_codex_notifications(
    paths: AppPaths,
    auth: AuthState,
    telegram: TelegramClient,
    recorder: Recorder,
    codex,
    runtime: ServiceRuntime | None = None,
    runtime_state: RuntimeState | None = None,
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
        if method in {"assistant/message.delta", "item/updated", "turn/output"}:
            text = extract_assistant_text(params)
            session = resolve_notification_session(session_store, auth, params)
            if session is not None and text:
                session_store.append_pending_output(session, text)
            continue
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
                telegram.send_message(auth.telegram_chat_id, "Codex login completed. Telegram and Codex are ready.")
            continue
        if method == "assistant/message.partial":
            text = extract_assistant_text(params)
            session = resolve_notification_session(session_store, auth, params)
            if session is not None:
                if text:
                    session_store.append_pending_output(session, text)
                flush_buffer(session.session_id, auth, telegram, recorder, session_store, mark_agent=False)
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
            if not assistant_text and session.thread_id and hasattr(codex, "read_thread"):
                try:
                    assistant_text = extract_latest_agent_message(codex.read_thread(session.thread_id, include_turns=True))
                except Exception:
                    assistant_text = None
            if assistant_text:
                session_store.append_pending_output(session, assistant_text)
            session.active_turn_id = None
            session.last_completed_turn_id = str(turn_id)
            session.status = "ACTIVE"
            session_store.save_session(session)
            flush_buffer(session.session_id, auth, telegram, recorder, session_store, mark_agent=True)
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
):
    if not is_auth_paired(auth) or codex is not None:
        return codex
    return start_codex_session_fn(config, auth, runtime, runtime_state, metadata, app_lock, telegram, handle_output)


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
            telegram.send_message(auth.telegram_chat_id, f"[{source}] {line[:3500]}")

    codex = None
    last_typing_sent_at: datetime | None = None
    codex_restart_failures = 0
    next_codex_restart_at = 0.0
    telegram_failures = 0
    next_telegram_poll_at = 0.0
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
    )
    if codex is None and is_auth_paired(auth):
        codex_restart_failures = 1
        next_codex_restart_at = time.monotonic() + codex_restart_delay(config, codex_restart_failures)
    save_json(paths.runtime, runtime_state.to_dict())
    append_recovery_log(paths.recovery_log, f"service started session_id={runtime_state.session_id}")
    if stale_approvals and auth.telegram_chat_id:
        telegram.send_message(
            auth.telegram_chat_id,
            f"{stale_approvals} pending approval request(s) were marked stale after restart.",
        )

    offset = None
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
            )
        while True:
            updates: list[dict] = []
            now = time.monotonic()
            if now >= next_telegram_poll_at:
                try:
                    updates = telegram.get_updates(offset=offset, timeout=20)
                    if runtime_state.telegram_state != "RUNNING":
                        runtime_state.telegram_state = "RUNNING"
                        save_json(paths.runtime, runtime_state.to_dict())
                    telegram_failures = 0
                    next_telegram_poll_at = 0.0
                except TelegramError as exc:
                    telegram_failures += 1
                    delay = telegram_retry_delay(config, telegram_failures)
                    next_telegram_poll_at = now + delay
                    runtime_state.telegram_state = "BACKOFF"
                    save_json(paths.runtime, runtime_state.to_dict())
                    append_recovery_log(paths.recovery_log, f"telegram poll failed -> backoff={delay:.1f}s error={exc}")
            for update in updates:
                offset = update["update_id"] + 1
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
                )
                drain_codex_approvals(paths, auth, telegram, codex)
                drain_codex_notifications(paths, auth, telegram, recorder, codex, runtime, runtime_state)
                flush_idle_partial_outputs(
                    paths,
                    auth,
                    telegram,
                    recorder,
                    session_store,
                    idle_seconds=config.partial_flush_idle_seconds,
                )
                last_typing_sent_at = maybe_send_typing_indicator(
                    paths,
                    auth,
                    telegram,
                    session_store,
                    interval_seconds=config.typing_indicator_interval_seconds,
                    last_sent_at=last_typing_sent_at,
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
                )
            drain_codex_approvals(paths, auth, telegram, codex)
            drain_codex_notifications(paths, auth, telegram, recorder, codex, runtime, runtime_state)
            flush_idle_partial_outputs(
                paths,
                auth,
                telegram,
                recorder,
                session_store,
                idle_seconds=config.partial_flush_idle_seconds,
            )
            last_typing_sent_at = maybe_send_typing_indicator(
                paths,
                auth,
                telegram,
                session_store,
                interval_seconds=config.typing_indicator_interval_seconds,
                last_sent_at=last_typing_sent_at,
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
            )
            time.sleep(config.poll_interval_seconds)
    finally:
        if codex is not None:
            codex.stop()
            runtime.stop_codex()
        recorder.stop()
        debug.stop()
        app_lock.clear()
        append_recovery_log(paths.recovery_log, "service stopped")
