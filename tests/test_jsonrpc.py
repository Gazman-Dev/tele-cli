from __future__ import annotations

import unittest

from runtime.jsonrpc import JsonRpcClient, JsonRpcError
from tests.fakes.fake_app_server import FakeAppServer, InMemoryJsonRpcTransport


class JsonRpcTests(unittest.TestCase):
    def test_request_correlates_response_after_interleaved_notification(self) -> None:
        transport = InMemoryJsonRpcTransport()
        server = FakeAppServer(transport)

        def handle_initialize(payload):
            server.notify("thread/updated", {"threadId": "thread-1"})
            return {"protocolVersion": "1.0", "capabilities": {"threads": True}}

        server.on("initialize", handle_initialize)
        client = JsonRpcClient(transport)
        client.start()
        try:
            result = client.request(
                "initialize",
                {
                    "protocolVersion": "2026-02-04",
                    "clientInfo": {"name": "tele-cli", "version": "0.1.0"},
                    "capabilities": {},
                },
            )
            notification = client.get_notification(timeout=1)
        finally:
            client.close()

        self.assertEqual(result["protocolVersion"], "1.0")
        self.assertEqual(notification.method, "thread/updated")
        self.assertEqual(notification.params, {"threadId": "thread-1"})

    def test_server_initiated_request_is_routed_separately_from_notifications(self) -> None:
        transport = InMemoryJsonRpcTransport()
        server = FakeAppServer(transport)
        client = JsonRpcClient(transport)
        client.start()
        try:
            server.request(77, "approval/request", {"tool": "shell", "command": "ls"})
            request = client.get_request(timeout=1)
        finally:
            client.close()

        self.assertEqual(request.id, 77)
        self.assertEqual(request.method, "approval/request")
        self.assertEqual(request.params, {"tool": "shell", "command": "ls"})

    def test_request_raises_on_jsonrpc_error(self) -> None:
        transport = InMemoryJsonRpcTransport()
        server = FakeAppServer(transport)

        def handle_thread_resume(payload):
            server.error(payload["id"], -32001, "thread not found")
            return None

        server.on("thread/resume", handle_thread_resume)
        client = JsonRpcClient(transport)
        client.start()
        try:
            with self.assertRaises(JsonRpcError):
                client.request("thread/resume", {"threadId": "missing"})
        finally:
            client.close()

    def test_client_can_respond_to_server_initiated_request(self) -> None:
        transport = InMemoryJsonRpcTransport()
        server = FakeAppServer(transport)
        client = JsonRpcClient(transport)
        client.start()
        try:
            server.request(77, "approval/request", {"tool": "shell"})
            request = client.get_request(timeout=1)
            client.respond(request.id, {"approved": True})
        finally:
            client.close()

        self.assertEqual(server.responses[-1]["id"], 77)
        self.assertEqual(server.responses[-1]["result"], {"approved": True})


if __name__ == "__main__":
    unittest.main()
