from __future__ import annotations

from .demo_ui import Colors, DemoExit, DemoState, MenuItem, TeleCliUxDemo, TerminalUI, terminal_size, visible_len

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
