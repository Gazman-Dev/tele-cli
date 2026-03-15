from __future__ import annotations

import unittest

from runtime.app_server_client import AppServerClient
from runtime.jsonrpc import JsonRpcClient
from tests.fakes.fake_app_server import FakeAppServer, InMemoryJsonRpcTransport


class AppServerClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.transport = InMemoryJsonRpcTransport()
        self.server = FakeAppServer(self.transport)
        self.rpc = JsonRpcClient(self.transport)
        self.rpc.start()
        self.client = AppServerClient(self.rpc)

    def tearDown(self) -> None:
        self.rpc.close()

    def test_initialize_returns_protocol_metadata(self) -> None:
        self.server.on(
            "initialize",
            lambda payload: {
                "protocolVersion": "2026-02-04",
                "capabilities": {"threads": True, "turns": True},
            },
        )

        result = self.client.initialize()

        self.assertEqual(result["protocolVersion"], "2026-02-04")
        self.assertTrue(result["capabilities"]["threads"])

    def test_get_account_returns_auth_state(self) -> None:
        self.server.on(
            "getAccount",
            lambda payload: {"status": "auth_required", "supports": ["chatgpt", "apiKey"]},
        )

        result = self.client.get_account()

        self.assertEqual(result["status"], "auth_required")
        self.assertIn("chatgpt", result["supports"])

    def test_login_account_requests_chatgpt_login_flow(self) -> None:
        self.server.on(
            "login/account",
            lambda payload: {"type": payload["params"]["type"], "authUrl": "https://example.test/login"},
        )

        result = self.client.login_account("chatgpt")

        self.assertEqual(result["type"], "chatgpt")
        self.assertEqual(result["authUrl"], "https://example.test/login")
        self.assertEqual(self.server.received[0]["method"], "login/account")
        self.assertEqual(self.server.received[0]["params"]["type"], "chatgpt")

    def test_thread_start_and_resume_forward_expected_params(self) -> None:
        self.server.on("thread/start", lambda payload: {"threadId": "thread-1"})
        self.server.on("thread/resume", lambda payload: {"threadId": payload["params"]["threadId"]})

        started = self.client.thread_start(model="gpt-5.3-codex", cwd="/repo")
        resumed = self.client.thread_resume("thread-1")

        self.assertEqual(started["threadId"], "thread-1")
        self.assertEqual(resumed["threadId"], "thread-1")
        self.assertEqual(self.server.received[0]["params"]["model"], "gpt-5.3-codex")
        self.assertEqual(self.server.received[1]["params"]["threadId"], "thread-1")

    def test_turn_start_sends_thread_id_and_input(self) -> None:
        self.server.on("turn/start", lambda payload: {"turnId": "turn-1"})

        result = self.client.turn_start("thread-1", "hello")

        self.assertEqual(result["turnId"], "turn-1")
        self.assertEqual(self.server.received[0]["params"]["threadId"], "thread-1")
        self.assertEqual(self.server.received[0]["params"]["input"], "hello")


if __name__ == "__main__":
    unittest.main()
