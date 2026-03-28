from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from core.json_store import save_json
from core.models import AuthState, CodexServerState, Config, RuntimeState
from core.paths import AppPaths
from core.state_versions import load_versioned_state, save_versioned_state

from .app_server_client import AppServerClient
from .approval_store import ApprovalRecord
from .instructions import build_instruction_paths, render_session_instructions
from .app_server_process import SubprocessJsonRpcTransport
from .jsonrpc import JsonRpcClient, JsonRpcNotification, JsonRpcTransport
from .performance import PerformanceTracker
from .runtime import ServiceRuntime
from .session_store import SessionStore
from .sleep import build_refresh_instructions, current_generation


STALE_ACTIVE_TURN_SECONDS = 30.0


def parse_session_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def is_stale_active_turn(session, *, now: datetime | None = None, threshold_seconds: float = STALE_ACTIVE_TURN_SECONDS) -> bool:
    if not session.active_turn_id:
        return False
    now = now or datetime.now(timezone.utc)
    candidates = [
        parse_session_timestamp(session.last_user_message_at),
        parse_session_timestamp(session.pending_output_updated_at),
        parse_session_timestamp(session.last_agent_message_at),
    ]
    last_activity = max((candidate for candidate in candidates if candidate is not None), default=None)
    if last_activity is None:
        return False
    return (now - last_activity).total_seconds() >= threshold_seconds


def normalize_initialize_result(initialize_result: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(initialize_result)
    capabilities = normalized.get("capabilities")
    if not isinstance(capabilities, dict):
        capabilities = {}
    user_agent = normalized.get("userAgent")
    if isinstance(user_agent, str) and user_agent.strip():
        normalized.setdefault("protocolVersion", "user-agent-only")
        capabilities.setdefault("threads", True)
    normalized["capabilities"] = capabilities
    return normalized


def validate_initialize_result(initialize_result: dict[str, Any]) -> None:
    normalized = normalize_initialize_result(initialize_result)
    protocol_version = normalized.get("protocolVersion")
    if not isinstance(protocol_version, str) or not protocol_version.strip():
        raise RuntimeError("App server did not report a usable protocol version.")
    capabilities = normalized.get("capabilities")
    if not isinstance(capabilities, dict):
        raise RuntimeError("App server did not report capabilities.")
    if not capabilities.get("threads"):
        raise RuntimeError("App server does not support thread lifecycle operations.")


def derive_codex_state(account: dict[str, Any]) -> str:
    account_info = account.get("account")
    if isinstance(account_info, dict) and (
        account_info.get("accountType")
        or account_info.get("type")
        or account.get("accountType")
        or account.get("type")
    ):
        return "RUNNING"
    if account.get("requiresOpenaiAuth") is True:
        return "AUTH_REQUIRED"
    status = str(account.get("status") or account.get("state") or "").lower()
    if status in {"auth_required", "login_required", "expired"}:
        return "AUTH_REQUIRED"
    return "RUNNING"


class AppServerSession:
    def __init__(
        self,
        client: AppServerClient,
        session_store: SessionStore,
        auth: AuthState,
        config: Config,
        performance: PerformanceTracker | None = None,
    ):
        self.client = client
        self.session_store = session_store
        self.auth = auth
        self.config = config
        self.performance = performance
        self._resumed_threads: set[str] = set()

    def _build_turn_input(self, session, text: str, *, recovered_from_error: bool = False) -> str:
        user_request = text
        if recovered_from_error:
            user_request = (
                "System: recovered from error, the previous message got interrupted.\n\n"
                "---\n\n"
                f"{text}"
            )
        if not session.instructions_dirty:
            return user_request
        if not session.thread_id:
            instructions = render_session_instructions(self.session_store.paths, session, refresh_reason="session_start")
            if not instructions:
                return user_request
            return f"{instructions}\n\nUser request:\n{user_request}"
        refresh, generation = build_refresh_instructions(self.session_store.paths, session)
        if refresh:
            session.last_seen_generation = generation
            return f"{refresh}\n\nUser request:\n{user_request}"
        return user_request

    def _scoped_auth(self, *, chat_id: int | None = None, user_id: int | None = None) -> AuthState:
        scoped = AuthState.from_dict(self.auth.to_dict())
        if chat_id is not None:
            scoped.telegram_chat_id = chat_id
        if user_id is not None:
            scoped.telegram_user_id = user_id
        return scoped

    def _start_or_steer_turn(self, session, text: str) -> bool:
        workspace_cwd = str(build_instruction_paths(self.session_store.paths).repo_root)
        if session.status == "RECOVERING_TURN":
            raise RuntimeError("Session is recovering an in-flight turn.")
        stale_active_turn = bool(session.active_turn_id and is_stale_active_turn(session))
        self.session_store.mark_user_message(session)
        if self.performance is not None:
            self.performance.mark_ai_dispatch_started(session)
        if session.status != "ARCHIVED":
            session.status = "ACTIVE"
        thread_id = session.thread_id
        recovered_from_error = False
        if session.active_turn_id:
            if stale_active_turn:
                self.client.turn_interrupt(session.active_turn_id)
                session.active_turn_id = None
                session.pending_output_text = ""
                session.pending_output_updated_at = None
                session.streaming_message_id = None
                session.streaming_output_text = ""
                session.thinking_message_text = ""
                session.status = "ACTIVE"
                self.session_store.save_session(session)
                recovered_from_error = True
            else:
                self.client.turn_steer(session.active_turn_id, text)
                session.status = "RUNNING_TURN"
                self.session_store.save_session(session)
                if self.performance is not None:
                    self.performance.mark_turn_registered(session)
                return False
        turn_input = self._build_turn_input(session, text, recovered_from_error=recovered_from_error)
        if thread_id:
            if thread_id not in self._resumed_threads:
                try:
                    resumed = self.client.thread_resume(thread_id)
                except Exception:
                    session.thread_id = None
                    self.session_store.save_session(session)
                    thread_id = None
                else:
                    thread_id = resumed.get("threadId") or thread_id
                    session.thread_id = thread_id
                    self._resumed_threads.add(thread_id)
                    if self.performance is not None:
                        self.performance.mark_thread_ready(session, trigger="thread_resume")
        else:
            started = self.client.thread_start(
                cwd=workspace_cwd,
                sandbox=self.config.sandbox_mode,
                approvalPolicy=self.config.approval_policy,
                personality=self.config.codex_personality,
            )
            thread_id = started.get("threadId")
            session.thread_id = thread_id
            self.session_store.save_session(session)
            if thread_id:
                self._resumed_threads.add(thread_id)
                if self.performance is not None:
                    self.performance.mark_thread_ready(session, trigger="thread_start")
        if not thread_id:
            started = self.client.thread_start(
                cwd=workspace_cwd,
                sandbox=self.config.sandbox_mode,
                approvalPolicy=self.config.approval_policy,
                personality=self.config.codex_personality,
            )
            thread_id = started.get("threadId")
            session.thread_id = thread_id
            self.session_store.save_session(session)
            if thread_id:
                self._resumed_threads.add(thread_id)
                if self.performance is not None:
                    self.performance.mark_thread_ready(session, trigger="thread_start_retry")
        if not thread_id:
            raise RuntimeError("App server did not return a thread id.")
        session.last_delivered_output_text = ""
        session.streaming_message_id = None
        session.streaming_output_text = ""
        session.thinking_message_text = ""
        self.session_store.save_session(session)
        turn = self.client.turn_start(
            thread_id,
            turn_input,
            cwd=workspace_cwd,
            approvalPolicy=self.config.approval_policy,
            sandboxPolicy=self.config.sandbox_mode,
            personality=self.config.codex_personality,
        )
        session.active_turn_id = turn.get("turnId")
        session.pending_output_text = ""
        session.status = "RUNNING_TURN"
        session.instructions_dirty = False
        session.last_seen_generation = current_generation(self.session_store.paths)
        self.session_store.save_session(session)
        if self.performance is not None:
            self.performance.mark_turn_registered(session)
        return recovered_from_error

    def send(
        self,
        text: str,
        topic_id: int | None = None,
        *,
        chat_id: int | None = None,
        user_id: int | None = None,
    ) -> bool:
        scoped_auth = self._scoped_auth(chat_id=chat_id, user_id=user_id)
        session = self.session_store.get_or_create_telegram_session(scoped_auth, topic_id)
        return self._start_or_steer_turn(session, text)

    def send_local(self, channel: str, text: str) -> bool:
        session = self.session_store.get_or_create_local_session(channel)
        return self._start_or_steer_turn(session, text)

    def interrupt(self, topic_id: int | None = None, *, chat_id: int | None = None, user_id: int | None = None) -> bool:
        scoped_auth = self._scoped_auth(chat_id=chat_id, user_id=user_id)
        session = self.session_store.get_current_telegram_session(scoped_auth, topic_id)
        if session is None or not session.active_turn_id:
            return False
        self.client.turn_interrupt(session.active_turn_id)
        session.active_turn_id = None
        session.pending_output_text = ""
        session.status = "INTERRUPTED"
        self.session_store.save_session(session)
        return True

    def interrupt_local(self, channel: str) -> bool:
        session = self.session_store.get_current_local_session(channel)
        if session is None or not session.active_turn_id:
            return False
        self.client.turn_interrupt(session.active_turn_id)
        session.active_turn_id = None
        session.pending_output_text = ""
        session.status = "INTERRUPTED"
        self.session_store.save_session(session)
        return True

    def poll_approval_request(self) -> ApprovalRecord | None:
        request = self.client.rpc.get_request_nowait()
        if request is None:
            return None
        return ApprovalRecord(
            request_id=request.id,
            method=request.method,
            params=request.params or {},
        )

    def approve(self, request_id: int) -> None:
        self.client.rpc.respond(request_id, {"approved": True})

    def deny(self, request_id: int) -> None:
        self.client.rpc.respond_error(request_id, -32002, "Denied by operator")

    def poll_notification(self) -> JsonRpcNotification | None:
        return self.client.rpc.get_notification_nowait()

    def stop(self) -> None:
        self.client.rpc.close()

    def is_alive(self) -> bool:
        transport = self.client.rpc.transport
        if hasattr(transport, "is_alive"):
            return bool(transport.is_alive())
        return True

    def read_thread(self, thread_id: str, include_turns: bool = True) -> dict[str, Any]:
        return self.client.thread_read(thread_id, include_turns=include_turns)


def recover_inflight_sessions(client: AppServerClient, session_store: SessionStore) -> None:
    del client
    state = session_store.load()
    changed = False
    for session in state.sessions:
        if session.status == "RECOVERING_TURN" and session.active_turn_id:
            session.status = "RUNNING_TURN"
            changed = True
        elif session.status == "RECOVERING_TURN":
            session.status = "ACTIVE"
            changed = True
    if changed:
        session_store.save(state)


def build_codex_server_state(
    *,
    transport: str,
    initialize_result: dict[str, Any],
    account_result: dict[str, Any],
    login_result: dict[str, Any] | None = None,
    pid: int | None = None,
    last_error: str | None = None,
) -> CodexServerState:
    login_payload = login_result or {}
    account_info = account_result.get("account") if isinstance(account_result.get("account"), dict) else {}
    return CodexServerState(
        transport=transport,
        initialized=True,
        protocol_version=initialize_result.get("protocolVersion"),
        account_status=account_result.get("status") or account_result.get("state"),
        account_type=account_result.get("accountType") or account_result.get("type") or account_info.get("accountType") or account_info.get("type"),
        auth_required=derive_codex_state(account_result) == "AUTH_REQUIRED",
        login_type=login_payload.get("type") or login_payload.get("loginType"),
        login_url=login_payload.get("url") or login_payload.get("authUrl") or login_payload.get("loginUrl"),
        pid=pid,
        capabilities=initialize_result.get("capabilities") or {},
        last_error=last_error,
    )


def build_failed_codex_server_state(
    *,
    transport: str,
    last_error: str,
    pid: int | None = None,
) -> CodexServerState:
    return CodexServerState(
        transport=transport,
        initialized=False,
        pid=pid,
        last_error=last_error,
    )


def bootstrap_app_server_session(
    *,
    paths: AppPaths,
    auth: AuthState,
    runtime: ServiceRuntime,
    runtime_state: RuntimeState,
    transport: JsonRpcTransport,
    config: Config,
    transport_name: str = "stdio://",
    client_factory: Callable[[JsonRpcClient], AppServerClient] = AppServerClient,
    performance: PerformanceTracker | None = None,
) -> AppServerSession:
    rpc = JsonRpcClient(transport)
    rpc.start()
    client = client_factory(rpc)
    initialize_result = normalize_initialize_result(client.initialize())
    validate_initialize_result(initialize_result)
    account_result = client.get_account()
    login_result: dict[str, Any] | None = None
    if derive_codex_state(account_result) == "AUTH_REQUIRED":
        try:
            login_result = client.login_account("chatgpt")
        except Exception:
            login_result = None
    session_store = SessionStore(paths)
    recover_inflight_sessions(client, session_store)
    codex_server_state = build_codex_server_state(
        transport=transport_name,
        initialize_result=initialize_result,
        account_result=account_result,
        login_result=login_result,
    )
    save_versioned_state(paths.codex_server, codex_server_state.to_dict())
    runtime.set_codex_state(derive_codex_state(account_result))
    save_json(paths.runtime, runtime_state.to_dict())
    return AppServerSession(client, session_store, auth, config, performance=performance)


def make_app_server_start_fn(
    paths: AppPaths,
    transport_factory: Callable[[Config, AuthState], JsonRpcTransport],
    transport_name: str = "stdio://",
):
    def start_app_server_session(
        config: Config,
        auth: AuthState,
        runtime: ServiceRuntime,
        runtime_state: RuntimeState,
        metadata,
        app_lock,
        telegram,
        handle_output,
        performance: PerformanceTracker | None = None,
    ) -> AppServerSession:
        transport = None
        try:
            transport = transport_factory(config, auth)
            session = bootstrap_app_server_session(
                paths=paths,
                auth=auth,
                runtime=runtime,
                runtime_state=runtime_state,
                transport=transport,
                config=config,
                transport_name=transport_name,
                performance=performance,
            )
            return session
        except Exception as exc:
            if transport is not None:
                try:
                    transport.close()
                except Exception:
                    pass
            last_error = str(exc)
            runtime.set_codex_state("DEGRADED")
            save_json(paths.runtime, runtime_state.to_dict())
            save_versioned_state(
                paths.codex_server,
                build_failed_codex_server_state(transport=transport_name, last_error=last_error).to_dict(),
            )
            return None

    return start_app_server_session


def build_app_server_command(config: Config) -> list[str]:
    return [*config.codex_command, "app-server", "--listen", "stdio://"]


def default_transport_factory(config: Config, auth: AuthState) -> JsonRpcTransport:
    return SubprocessJsonRpcTransport.start(build_app_server_command(config))
