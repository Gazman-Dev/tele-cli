from __future__ import annotations

import threading
import time
import unittest
from io import BytesIO
from unittest.mock import patch
import urllib.error

from integrations.telegram import TelegramClient, TelegramError


class TelegramClientTests(unittest.TestCase):
    def test_outbound_requests_are_serialized_across_threads(self) -> None:
        client = TelegramClient("token")
        timeline: list[tuple[str, str, float]] = []
        timeline_lock = threading.Lock()

        def fake_request(method: str, params=None, *, data=None, headers=None):
            start = time.monotonic()
            with timeline_lock:
                timeline.append(("start", method, start))
            time.sleep(0.05)
            end = time.monotonic()
            with timeline_lock:
                timeline.append(("end", method, end))
            if method == "sendMessage":
                return {"message_id": 1}
            if method == "editMessageText":
                return {"message_id": 1}
            return {"ok": True}

        client._request = fake_request  # type: ignore[method-assign]

        first = threading.Thread(target=lambda: client.send_message(1, "hello"))
        second = threading.Thread(target=lambda: client.edit_message_text(1, 1, "world"))

        first.start()
        second.start()
        first.join()
        second.join()

        starts = [(method, ts) for phase, method, ts in timeline if phase == "start"]
        ends = [(method, ts) for phase, method, ts in timeline if phase == "end"]

        self.assertEqual(len(starts), 2)
        self.assertEqual(len(ends), 2)

        start_times = {method: ts for method, ts in starts}
        end_times = {method: ts for method, ts in ends}
        ordered_methods = [method for method, _ in sorted(starts, key=lambda item: item[1])]
        first_method, second_method = ordered_methods

        self.assertGreaterEqual(start_times[second_method], end_times[first_method] - 0.005)

    def test_http_error_includes_telegram_response_body(self) -> None:
        client = TelegramClient("token")
        error = urllib.error.HTTPError(
            url="https://api.telegram.org/bottoken/sendMessage",
            code=400,
            msg="Bad Request",
            hdrs=None,
            fp=BytesIO(b'{"ok": false, "error_code": 400, "description": "Bad Request: chat not found"}'),
        )

        with patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(TelegramError) as raised:
                client.send_message(1, "hello")

        self.assertIn("chat not found", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
