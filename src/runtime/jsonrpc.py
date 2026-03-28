from __future__ import annotations

import json
import queue
import threading
from dataclasses import dataclass
from typing import Any, Protocol


class JsonRpcError(RuntimeError):
    pass


class JsonRpcTransport(Protocol):
    def read_line(self, timeout: float | None = None) -> str | None:
        raise NotImplementedError

    def write_line(self, line: str) -> None:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError


@dataclass(frozen=True)
class JsonRpcNotification:
    method: str
    params: dict[str, Any] | None = None


@dataclass(frozen=True)
class JsonRpcRequest:
    id: int
    method: str
    params: dict[str, Any] | None = None


class JsonRpcClient:
    def __init__(self, transport: JsonRpcTransport):
        self.transport = transport
        self._next_id = 1
        self._pending: dict[int, queue.Queue[dict[str, Any]]] = {}
        self._pending_lock = threading.Lock()
        self._notifications: queue.Queue[JsonRpcNotification] = queue.Queue()
        self._requests: queue.Queue[JsonRpcRequest] = queue.Queue()
        self._stop_event = threading.Event()
        self._reader = threading.Thread(target=self._reader_loop, daemon=True)
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._reader.start()

    def close(self) -> None:
        self._stop_event.set()
        self.transport.close()
        if self._reader.is_alive():
            self._reader.join(timeout=1)

    def request(self, method: str, params: dict[str, Any] | None = None, timeout: float = 5.0) -> dict[str, Any]:
        request_id = self._allocate_id()
        response_queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
        with self._pending_lock:
            self._pending[request_id] = response_queue
        self.transport.write_line(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": method,
                    "params": params or {},
                }
            )
        )
        try:
            response = response_queue.get(timeout=timeout)
        except queue.Empty as exc:
            raise TimeoutError(f"Timed out waiting for JSON-RPC response to {method}.") from exc
        finally:
            with self._pending_lock:
                self._pending.pop(request_id, None)
        if "error" in response:
            raise JsonRpcError(str(response["error"]))
        return response.get("result", {})

    def get_notification(self, timeout: float | None = None) -> JsonRpcNotification:
        return self._notifications.get(timeout=timeout)

    def get_notification_nowait(self) -> JsonRpcNotification | None:
        try:
            return self._notifications.get_nowait()
        except queue.Empty:
            return None

    def get_request(self, timeout: float | None = None) -> JsonRpcRequest:
        return self._requests.get(timeout=timeout)

    def get_request_nowait(self) -> JsonRpcRequest | None:
        try:
            return self._requests.get_nowait()
        except queue.Empty:
            return None

    def respond(self, request_id: int, result: dict[str, Any] | None = None) -> None:
        self.transport.write_line(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": result or {},
                }
            )
        )

    def respond_error(self, request_id: int, code: int, message: str) -> None:
        self.transport.write_line(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": code, "message": message},
                }
            )
        )

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        self.transport.write_line(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "method": method,
                    "params": params or {},
                }
            )
        )

    def _allocate_id(self) -> int:
        request_id = self._next_id
        self._next_id += 1
        return request_id

    def _reader_loop(self) -> None:
        while not self._stop_event.is_set():
            line = self.transport.read_line(timeout=0.1)
            if line is None:
                continue
            payload = json.loads(line)
            if "method" in payload and "id" in payload:
                self._requests.put(
                    JsonRpcRequest(
                        id=int(payload["id"]),
                        method=payload["method"],
                        params=payload.get("params"),
                    )
                )
                continue
            if "method" in payload:
                self._notifications.put(
                    JsonRpcNotification(
                        method=payload["method"],
                        params=payload.get("params"),
                    )
                )
                continue
            response_id = payload.get("id")
            if response_id is None:
                continue
            with self._pending_lock:
                response_queue = self._pending.get(int(response_id))
            if response_queue is not None:
                response_queue.put(payload)
