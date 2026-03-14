from __future__ import annotations

from .app import TeleCliUxDemo
from .state import Colors, DemoExit, DemoState, MenuItem, terminal_size, visible_len
from .ui import TerminalUI

__all__ = [
    "Colors",
    "DemoExit",
    "DemoState",
    "MenuItem",
    "TeleCliUxDemo",
    "TerminalUI",
    "main",
    "terminal_size",
    "visible_len",
]


def main() -> None:
    TeleCliUxDemo().run()


if __name__ == "__main__":
    main()
