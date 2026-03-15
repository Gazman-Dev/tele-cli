from __future__ import annotations

import unittest
from unittest.mock import patch

from demo_ui.ui import TerminalUI


class _FakeStream:
    def __init__(self, *, is_tty: bool, encoding: str = "utf-8") -> None:
        self._is_tty = is_tty
        self.encoding = encoding

    def isatty(self) -> bool:
        return self._is_tty


class TerminalUiTests(unittest.TestCase):
    def test_terminal_ui_attaches_controlling_tty_when_stdio_is_piped(self) -> None:
        fake_in = _FakeStream(is_tty=False)
        fake_out = _FakeStream(is_tty=False)
        tty_in = _FakeStream(is_tty=True)
        tty_out = _FakeStream(is_tty=True)

        with (
            patch("demo_ui.ui.sys.stdin", fake_in),
            patch("demo_ui.ui.sys.stdout", fake_out),
            patch("demo_ui.ui.sys.stderr", fake_out),
            patch("demo_ui.ui.os.name", "posix"),
            patch("demo_ui.ui.open", side_effect=[tty_in, tty_out]),
        ):
            ui = TerminalUI()

        self.assertTrue(ui.is_tty)


if __name__ == "__main__":
    unittest.main()
