from __future__ import annotations

from typing import Any, Callable

from core.json_store import save_json
from core.models import AuthState, CodexServerState, Config, RuntimeState
from core.paths import AppPaths
from core.state_versions import load_versioned_state, save_versioned_state

from .app_server_client import AppServerClient
from .approval_store import ApprovalRecord
from .app_server_process import SubprocessJsonRpcTransport
from .jsonrpc import JsonRpcClient, JsonRpcNotification, JsonRpcTransport
from .runtime import ServiceRuntime
from .session_store import SessionStore


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
    def __init__(self, client: AppServerClient, session_store: SessionStore, auth: AuthState, config: Config):
        self.client = client
        self.session_store = session_store
        self.auth = auth
        self.config = config
        self._resumed_threads: set[str] = set()

    def send(self, text: str, topic_id: int | None = None) -> None:
        session = self.session_store.get_or_create_telegram_session(self.auth, topic_id)
        if session.status == "RECOVERING_TURN":
            raise RuntimeError("Session is recovering an in-flight turn.")
        self.session_store.mark_user_message(session)
        if session.status != "ARCHIVED":
            session.status = "ACTIVE"
        thread_id = session.thread_id
        if session.active_turn_id:
            self.client.turn_steer(session.active_turn_id, text)
            session.status = "RUNNING_TURN"
            self.session_store.save_session(session)
            return
        if thread_id:
            if thread_id not in self._resumed_threads:
                resumed = self.client.thread_resume(thread_id)
                thread_id = resumed.get("threadId") or thread_id
                self._resumed_threads.add(thread_id)
        else:
            started = self.client.thread_start(
                cwd=self.config.state_dir,
                sandbox=self.config.sandbox_mode,
                approvalPolicy=self.config.approval_policy,
            )
            thread_id = started.get("threadId")
            session.thread_id = thread_id
            self.session_store.save_session(session)
            if thread_id:
                self._resumed_threads.add(thread_id)
        if not thread_id:
            raise RuntimeError("App server did not return a thread id.")
        turn = self.client.turn_start(thread_id, text)
        session.active_turn_id = turn.get("turnId")
        session.pending_output_text = ""
        session.status = "RUNNING_TURN"
        self.session_store.save_session(session)

    def interrupt(self, topic_id: int | None = None) -> bool:
        session = self.session_store.get_current_telegram_session(self.auth, topic_id)
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


def recover_inflight_sessions(client: AppServerClient, session_store: SessionStore) -> None:
    for session in session_store.mark_recovering_turns():
        if not session.thread_id:
            continue
        try:
            resumed = client.thread_resume(session.thread_id)
        except Exception:
            continue
        resumed_thread_id = resumed.get("threadId") or session.thread_id
        session.thread_id = resumed_thread_id
        session.status = "RUNNING_TURN"
        session_store.save_session(session)


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
    return AppServerSession(client, session_store, auth, config)


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
            )
            if auth.telegram_chat_id:
                if runtime_state.codex_state == "AUTH_REQUIRED":
                    telegram.send_message(auth.telegram_chat_id, "Codex login is required. Telegram remains available.")
                    persisted = load_versioned_state(paths.codex_server, CodexServerState.from_dict)
                    if persisted is not None and persisted.login_url:
                        telegram.send_message(auth.telegram_chat_id, f"Complete Codex login: {persisted.login_url}")
                else:
                    telegram.send_message(auth.telegram_chat_id, "Tele Cli service connected to Codex App Server.")
                if session.session_store.has_recovering_session(auth):
                    telegram.send_message(
                        auth.telegram_chat_id,
                        "A previous turn is still recovering after restart. This chat stays blocked until recovery finishes, /stop is used, or /new starts fresh.",
                    )
            return session
        except Exception as exc:
            if transport is not None:
                try:
                    transport.close()
                except Exception:
                    pass
            runtime.set_codex_state("DEGRADED")
            save_json(paths.runtime, runtime_state.to_dict())
            save_versioned_state(
                paths.codex_server,
                build_failed_codex_server_state(transport=transport_name, last_error=str(exc)).to_dict(),
            )
            if auth.telegram_chat_id:
                telegram.send_message(auth.telegram_chat_id, "Codex App Server failed to start. Telegram remains available.")
            return None

    return start_app_server_session


def build_app_server_command(config: Config) -> list[str]:
    return [*config.codex_command, "app-server", "--listen", "stdio://"]


def default_transport_factory(config: Config, auth: AuthState) -> JsonRpcTransport:
    return SubprocessJsonRpcTransport.start(build_app_server_command(config))
