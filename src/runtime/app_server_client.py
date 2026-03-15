from __future__ import annotations

from typing import Any

from .jsonrpc import JsonRpcClient


class AppServerClient:
    def __init__(self, rpc: JsonRpcClient):
        self.rpc = rpc

    def initialize(self, client_name: str = "tele-cli") -> dict[str, Any]:
        return self.rpc.request("initialize", {"client": client_name})

    def get_account(self) -> dict[str, Any]:
        return self.rpc.request("getAccount", {})

    def login_account(self, login_type: str = "chatgpt") -> dict[str, Any]:
        return self.rpc.request("login/account", {"type": login_type})

    def thread_start(self, **params: Any) -> dict[str, Any]:
        return self.rpc.request("thread/start", params)

    def thread_resume(self, thread_id: str) -> dict[str, Any]:
        return self.rpc.request("thread/resume", {"threadId": thread_id})

    def turn_start(self, thread_id: str, text: str) -> dict[str, Any]:
        return self.rpc.request("turn/start", {"threadId": thread_id, "input": text})

    def turn_steer(self, turn_id: str, text: str) -> dict[str, Any]:
        return self.rpc.request("turn/steer", {"turnId": turn_id, "input": text})

    def turn_interrupt(self, turn_id: str) -> dict[str, Any]:
        return self.rpc.request("turn/interrupt", {"turnId": turn_id})
