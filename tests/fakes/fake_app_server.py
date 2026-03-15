from __future__ import annotations

import json
import queue
from typing import Any, Callable


class InMemoryJsonRpcTransport:
    def __init__(self) -> None:
        self._incoming: queue.Queue[str] = queue.Queue()
        self._server: FakeAppServer | None = None
        self.closed = False

    def attach_server(self, server: "FakeAppServer") -> None:
        self._server = server

    def read_line(self, timeout: float | None = None) -> str | None:
        if self.closed:
            return None
        try:
            return self._incoming.get(timeout=timeout)
        except queue.Empty:
            return None

    def write_line(self, line: str) -> None:
        if self.closed:
            raise RuntimeError("transport is closed")
        if self._server is None:
            raise RuntimeError("no fake app server attached")
        self._server.receive(line)

    def emit(self, payload: dict[str, Any]) -> None:
        self._incoming.put(json.dumps(payload))

    def close(self) -> None:
        self.closed = True

    def is_alive(self) -> bool:
        return not self.closed


class FakeAppServer:
    def __init__(self, transport: InMemoryJsonRpcTransport):
        self.transport = transport
        self.transport.attach_server(self)
        self.handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any] | None]] = {}
        self.received: list[dict[str, Any]] = []
        self.responses: list[dict[str, Any]] = []

    def on(self, method: str, handler: Callable[[dict[str, Any]], dict[str, Any] | None]) -> None:
        self.handlers[method] = handler

    def receive(self, line: str) -> None:
        payload = json.loads(line)
        self.received.append(payload)
        if "method" not in payload:
            self.responses.append(payload)
            return
        method = payload["method"]
        handler = self.handlers.get(method)
        result = handler(payload) if handler else {}
        if payload.get("id") is not None:
            self.transport.emit({"jsonrpc": "2.0", "id": payload["id"], "result": result or {}})

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        self.transport.emit({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def request(self, request_id: int, method: str, params: dict[str, Any] | None = None) -> None:
        self.transport.emit(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params or {},
            }
        )

    def respond(self, request_id: int, result: dict[str, Any] | None = None) -> None:
        self.transport.emit({"jsonrpc": "2.0", "id": request_id, "result": result or {}})

    def error(self, request_id: int, code: int, message: str) -> None:
        self.transport.emit(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": code, "message": message},
            }
        )
