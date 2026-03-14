from __future__ import annotations

import time

from .state import Colors, DemoExit, DemoState, MenuItem
from .flows import (
    run_pairing_screen,
    run_token_screen,
    show_debug,
    show_service_restart,
    show_setup_complete,
    show_uninstall,
    show_update,
)
from .ui import TerminalUI


class TeleCliUxDemo:
    def __init__(self) -> None:
        self.state = DemoState()
        self.ui = TerminalUI()
        self.selection = 0

    def run(self) -> None:
        if not self.ui.is_tty:
            raise SystemExit("UX demo requires an interactive terminal.")
        self.ui.begin()
        try:
            self._startup_screen()
            self._status_loop()
        except KeyboardInterrupt:
            raise DemoExit(0)
        finally:
            self.ui.end()

    def _startup_screen(self) -> None:
        tasks = [
            "Loading local background service",
            "Synchronizing AI engine",
            "Wiring Telegram API handlers",
            "Installing required Python packages",
        ]
        durations = [0.32, 0.44, 0.48, 0.96]
        total_duration = max(2.0, sum(durations))

        start = time.time()
        frame = 0
        while True:
            elapsed = time.time() - start
            if elapsed >= total_duration:
                break

            remaining = elapsed
            active_index = len(tasks) - 1
            active_progress = 1.0
            completed = 0.0
            for index, duration in enumerate(durations):
                if remaining >= duration:
                    completed += duration
                    continue
                active_index = index
                active_progress = max(0.0, min(1.0, remaining / duration))
                break
            else:
                active_index = len(tasks) - 1
                active_progress = 1.0

            overall_progress = max(0.0, min(1.0, completed / total_duration + (active_progress * durations[active_index] / total_duration)))
            self.ui.render(
                self.ui.startup_progress_frame(
                    frame,
                    tasks,
                    active_index,
                    active_progress,
                    overall_progress,
                )
            )
            time.sleep(0.08)
            frame += 1

        self.ui.render(self.ui.startup_progress_frame(frame, tasks, len(tasks) - 1, 1.0, 1.0))
        time.sleep(0.2)
        self.state.status_line = "Setup required"

    def _telegram_token_screen(self) -> None:
        run_token_screen(self.ui, self.state)

    def _telegram_pairing_screen(self) -> None:
        run_pairing_screen(self.ui, self.state)

    def _setup_complete_screen(self) -> None:
        show_setup_complete(self.ui, self.state)

    def _status_loop(self) -> None:
        while self.state.running:
            items = self._menu_items()
            self.selection %= len(items)
            self._render_status_screen(items)
            key = self.ui.read_key()
            if key == "up":
                self.selection = (self.selection - 1) % len(items)
            elif key == "down":
                self.selection = (self.selection + 1) % len(items)
            elif key == "enter":
                self._run_action(items[self.selection].action)
            elif key in {"q", "esc"}:
                self.state.running = False

    def _render_status_screen(self, items: list[MenuItem]) -> None:
        menu_lines: list[str] =[]
        for index, item in enumerate(items):
            if index == self.selection:
                menu_lines.append(f"{Colors.chip_focus} > {item.label.ljust(28)} {Colors.reset}")
            else:
                menu_lines.append(f"  {item.label.ljust(29)}")

        self.ui.render(
            self.ui.print_header()
            + self.ui.system_strip(
                self.state.service_state,
                self.state.codex_state,
                self.state.telegram_state,
                self.state.status_line,
            )
            + [""]
            + self.ui.panel("Menu", menu_lines, width=54)
        )

    def _menu_items(self) -> list[MenuItem]:
        return[
            MenuItem("Status refresh", "refresh"),
            MenuItem("Setup", "setup"),
            MenuItem("Restart service", "service"),
            MenuItem("Debug mode", "debug"),
            MenuItem("Update Tele-Cli", "update"),
            MenuItem("Uninstall", "uninstall"),
            MenuItem("Quit", "quit"),
        ]

    def _run_action(self, action: str) -> None:
        if action == "refresh":
            self.state.status_line = "waiting for Telegram commands"
        elif action == "setup":
            self._telegram_token_screen()
            self._telegram_pairing_screen()
            self._setup_complete_screen()
        elif action == "service":
            self._service_restart_screen()
        elif action == "debug":
            self._debug_screen()
        elif action == "update":
            self._update_screen()
        elif action == "uninstall":
            self._uninstall_screen()
        elif action == "quit":
            self.state.running = False

    def _service_restart_screen(self) -> None:
        show_service_restart(self.ui, self.state)

    def _debug_screen(self) -> None:
        show_debug(self.ui, self.state)

    def _update_screen(self) -> None:
        show_update(self.ui, self.state)

    def _uninstall_screen(self) -> None:
        show_uninstall(self.ui, self.state)
