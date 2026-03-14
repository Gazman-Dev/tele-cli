from __future__ import annotations

from .ux_demo_app import TeleCliUxDemo
from .ux_demo_state import Colors, DemoExit, DemoState, MenuItem, terminal_size, visible_len
from .ux_demo_ui import TerminalUI

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
