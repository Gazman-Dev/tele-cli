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
        self.assertEqual(self.server.received[0]["params"]["protocolVersion"], "2026-02-04")
        self.assertEqual(self.server.received[0]["params"]["clientInfo"]["name"], "tele-cli")

    def test_get_account_returns_auth_state(self) -> None:
        self.server.on(
            "account/read",
            lambda payload: {"account": None, "requiresOpenaiAuth": True, "supports": ["chatgpt", "apiKey"]},
        )

        result = self.client.get_account()

        self.assertEqual(result["status"], "auth_required")
        self.assertIn("chatgpt", result["supports"])

    def test_get_account_prefers_real_account_over_requires_openai_auth_flag(self) -> None:
        self.server.on(
            "account/read",
            lambda payload: {
                "account": {"accountType": "chatgpt"},
                "requiresOpenaiAuth": True,
            },
        )

        result = self.client.get_account()

        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["accountType"], "chatgpt")

    def test_login_account_requests_chatgpt_login_flow(self) -> None:
        self.server.on(
            "account/login/start",
            lambda payload: {"type": payload["params"]["type"], "authUrl": "https://example.test/login"},
        )

        result = self.client.login_account("chatgpt")

        self.assertEqual(result["type"], "chatgpt")
        self.assertEqual(result["authUrl"], "https://example.test/login")
        self.assertEqual(self.server.received[0]["method"], "account/login/start")
        self.assertEqual(self.server.received[0]["params"]["type"], "chatgpt")

    def test_thread_start_and_resume_forward_expected_params(self) -> None:
        self.server.on("thread/start", lambda payload: {"thread": {"id": "thread-1"}})
        self.server.on("thread/resume", lambda payload: {"thread": {"id": payload["params"]["threadId"]}})

        started = self.client.thread_start(model="gpt-5.3-codex", cwd="/repo")
        resumed = self.client.thread_resume("thread-1")

        self.assertEqual(started["threadId"], "thread-1")
        self.assertEqual(resumed["threadId"], "thread-1")
        self.assertEqual(self.server.received[0]["params"]["model"], "gpt-5.3-codex")
        self.assertEqual(self.server.received[1]["params"]["threadId"], "thread-1")

    def test_turn_start_sends_thread_id_and_input(self) -> None:
        self.server.on("turn/start", lambda payload: {"turn": {"id": "turn-1"}})

        result = self.client.turn_start("thread-1", "hello")

        self.assertEqual(result["turnId"], "turn-1")
        self.assertEqual(self.server.received[0]["params"]["threadId"], "thread-1")
        self.assertEqual(self.server.received[0]["params"]["input"], [{"type": "text", "text": "hello"}])

    def test_turn_steer_sends_typed_text_input(self) -> None:
        self.server.on("turn/steer", lambda payload: {"turn": {"id": payload["params"]["turnId"]}})

        result = self.client.turn_steer("turn-1", "again")

        self.assertEqual(result["turnId"], "turn-1")
        self.assertEqual(self.server.received[0]["params"]["input"], [{"type": "text", "text": "again"}])

    def test_thread_read_requests_turns(self) -> None:
        self.server.on("thread/read", lambda payload: {"thread": {"id": payload["params"]["threadId"], "turns": []}})

        result = self.client.thread_read("thread-1")

        self.assertEqual(result["threadId"], "thread-1")
        self.assertTrue(self.server.received[0]["params"]["includeTurns"])

    def test_get_account_falls_back_to_legacy_method(self) -> None:
        self.server.on("account/read", lambda payload: {})
        self.server.on("getAccount", lambda payload: {"status": "ready", "accountType": "chatgpt"})

        result = self.client.get_account()

        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["accountType"], "chatgpt")

    def test_login_account_falls_back_to_legacy_method(self) -> None:
        self.server.on("account/login/start", lambda payload: {})
        self.server.on("login/account", lambda payload: {"type": payload["params"]["type"], "authUrl": "https://example.test/legacy"})

        result = self.client.login_account("chatgpt")

        self.assertEqual(result["authUrl"], "https://example.test/legacy")


if __name__ == "__main__":
    unittest.main()
