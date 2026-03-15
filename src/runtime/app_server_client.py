from __future__ import annotations

from typing import Any

from app_meta import APP_VERSION

from .jsonrpc import JsonRpcClient, JsonRpcError


def _is_unknown_variant_error(exc: JsonRpcError) -> bool:
    return "unknown variant" in str(exc).lower()


def _looks_like_account_payload(payload: dict[str, Any]) -> bool:
    return any(key in payload for key in ("status", "state", "requiresOpenaiAuth", "account"))


def _looks_like_login_payload(payload: dict[str, Any]) -> bool:
    return any(key in payload for key in ("type", "loginType", "authUrl", "loginUrl", "url"))


def _normalize_account_payload(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(payload)
    account = normalized.get("account")
    if isinstance(account, dict):
        normalized.setdefault("accountType", account.get("accountType") or account.get("type"))
        if "status" not in normalized and "state" not in normalized:
            normalized["status"] = account.get("status") or account.get("state") or "ready"
    requires_openai_auth = normalized.get("requiresOpenaiAuth")
    has_account = isinstance(account, dict) and bool(account)
    if requires_openai_auth is True and not has_account:
        normalized["status"] = "auth_required"
    elif requires_openai_auth is False and "status" not in normalized and "state" not in normalized:
        normalized["status"] = "ready"
    return normalized


def _normalize_thread_payload(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(payload)
    thread = normalized.get("thread")
    if isinstance(thread, dict):
        thread_id = thread.get("id")
        if isinstance(thread_id, str) and thread_id:
            normalized.setdefault("threadId", thread_id)
    return normalized


def _normalize_turn_payload(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(payload)
    turn = normalized.get("turn")
    if isinstance(turn, dict):
        turn_id = turn.get("id")
        if isinstance(turn_id, str) and turn_id:
            normalized.setdefault("turnId", turn_id)
    return normalized


class AppServerClient:
    def __init__(self, rpc: JsonRpcClient):
        self.rpc = rpc

    def initialize(self, client_name: str = "tele-cli") -> dict[str, Any]:
        return self.rpc.request(
            "initialize",
            {
                "protocolVersion": "2026-02-04",
                "clientInfo": {
                    "name": client_name,
                    "version": APP_VERSION,
                },
                "capabilities": {},
            },
        )

    def get_account(self) -> dict[str, Any]:
        try:
            result = _normalize_account_payload(self.rpc.request("account/read", {}))
            if _looks_like_account_payload(result):
                return result
        except JsonRpcError as exc:
            if not _is_unknown_variant_error(exc):
                raise
        return _normalize_account_payload(self.rpc.request("getAccount", {}))

    def login_account(self, login_type: str = "chatgpt") -> dict[str, Any]:
        try:
            result = self.rpc.request("account/login/start", {"type": login_type})
            if _looks_like_login_payload(result):
                return result
        except JsonRpcError as exc:
            if not _is_unknown_variant_error(exc):
                raise
        return self.rpc.request("login/account", {"type": login_type})

    def thread_start(self, **params: Any) -> dict[str, Any]:
        return _normalize_thread_payload(self.rpc.request("thread/start", params))

    def thread_resume(self, thread_id: str) -> dict[str, Any]:
        return _normalize_thread_payload(self.rpc.request("thread/resume", {"threadId": thread_id}))

    def turn_start(self, thread_id: str, text: str) -> dict[str, Any]:
        return _normalize_turn_payload(self.rpc.request("turn/start", {"threadId": thread_id, "input": text}))

    def turn_steer(self, turn_id: str, text: str) -> dict[str, Any]:
        return _normalize_turn_payload(self.rpc.request("turn/steer", {"turnId": turn_id, "input": text}))

    def turn_interrupt(self, turn_id: str) -> dict[str, Any]:
        return self.rpc.request("turn/interrupt", {"turnId": turn_id})
