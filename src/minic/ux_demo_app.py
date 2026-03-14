from __future__ import annotations

import random
import time

from .ux_demo_state import Colors, DemoExit, DemoState, MenuItem
from .ux_demo_ui import TerminalUI


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
        self._bridge_splash_screen()
        self.ui.render(
            self.ui.print_header()
            + self.ui.system_strip("starting", "not authenticated", "not paired", "Preparing the local bridge and first-run setup.")
            + [""]
            + self.ui.panel(
                "Installing Tele-Cli",
                [f"{Colors.muted}Preparing local service, dependencies, and bridge wiring.{Colors.reset}"],
                width=72,
                align="center",
            )
        )
        print()
        for step in [
            "Loading local background service",
            "Synchronizing AI engine",
            "Wiring Telegram API handlers",
            "Installing required Python packages",
        ]:
            self.ui.spinner(step, 0.75)
        self.state.status_line = "Setup required"

    def _bridge_splash_screen(self) -> None:
        start = time.time()
        frame = 0
        while time.time() - start < 3.0:
            self.ui.render(self.ui.splash_frame(frame))
            time.sleep(0.16)
            frame += 1

    def _telegram_token_screen(self) -> None:
        error = ""
        while True:
            lines = [
                "Connect Telegram to this machine.",
                "",
                "Create a bot with BotFather and paste the token here.",
                "",
                f"{Colors.muted}BotFather: https://t.me/BotFather{Colors.reset}",
            ]
            if error:
                lines.extend(["", f"{Colors.red}{error}{Colors.reset}"])

            self.ui.render(
                self.ui.print_header()
                + self.ui.panel("Telegram Bot Setup", lines, width=74)
                + self.ui.input_section("Paste bot token", 74, title="Bot Token")
            )
            token = self.ui.input_line("Paste bot token", panel_width=74, use_existing_field=True)
            if self._is_valid_token(token):
                self.state.token = token
                self.state.status_line = "Telegram token saved"
                self.ui.render(
                    self.ui.print_header()
                    + self.ui.panel("Telegram Bot Setup", [f"{Colors.green}{Colors.bold}Token saved.{Colors.reset}"], align="center")
                )
                time.sleep(0.9)
                return
            error = "Invalid Telegram token. In this demo, only x fails."

    def _telegram_pairing_screen(self) -> None:
        self.state.pairing_requested = False
        self.state.pairing_code = ""
        error = ""
        while True:
            lines = [
                "Pair this machine with your Telegram user.",
                "",
                "1. Send any message to your bot.",
                "2. The bot replies with your unique code.",
                "3. Enter that code on this machine.",
            ]
            if not self.state.pairing_requested:
                lines.extend(
                    [
                        "",
                        f"{Colors.muted}Waiting for the first Telegram message.{Colors.reset}",
                        "",
                        f"{Colors.muted}Demo: press m to simulate the bot reply.{Colors.reset}",
                    ]
                )
                self.ui.render(self.ui.print_header() + self.ui.panel("Telegram Pairing", lines, width=74, align="center"))
                key = self.ui.read_key()
                if key == "m":
                    self.state.pairing_requested = True
                    self.state.pairing_code = f"{random.randint(0, 999999):06d}"
                    self.state.status_line = "Telegram replied with a unique code"
                elif key == "esc":
                    raise DemoExit(0)
                continue

            lines.extend(
                [
                    "",
                    f"{Colors.muted}Telegram replied with this code{Colors.reset}",
                    f"{Colors.green}{Colors.bold}Telegram code{Colors.reset}",
                    f"{Colors.chip_focus}   {self.state.pairing_code}   {Colors.reset}",
                    "",
                    f"{Colors.muted}Enter it below to confirm this machine.{Colors.reset}",
                ]
            )
            if error:
                lines.extend(["", f"{Colors.red}{error}{Colors.reset}"])

            self.ui.render(
                self.ui.print_header()
                + self.ui.panel("Telegram Pairing", lines, width=74, align="center")
                + self.ui.input_section("Type the Telegram code", 74, title="Pairing Code")
            )
            entered_code = self.ui.input_line("Type the Telegram code", panel_width=74, use_existing_field=True)
            if entered_code.lower() == "x":
                error = "Invalid pairing code. Copy the code from the Telegram bot reply."
                continue
            if entered_code != self.state.pairing_code:
                error = "Code mismatch. Enter the exact code sent by the Telegram bot."
                continue

            self.state.telegram_state = "paired"
            self.state.status_line = "Device successfully paired"
            self.ui.render(
                self.ui.print_header()
                + self.ui.panel("Telegram Pairing", [f"{Colors.green}{Colors.bold}Device successfully paired.{Colors.reset}"], align="center")
            )
            time.sleep(0.9)
            return

    def _setup_complete_screen(self) -> None:
        self.state.configured = True
        self.state.service_state = "running"
        self.state.codex_state = "authenticated"
        self.state.telegram_state = "paired"
        self.state.status_line = "waiting for Telegram commands"
        self.ui.render(
            self.ui.print_header()
            + self.ui.panel(
                "Setup Complete",
                [f"{Colors.green}{Colors.bold}Setup complete.{Colors.reset}", "", "Starting Tele-Cli service..."],
                align="center",
            )
        )
        time.sleep(1.0)

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
        menu_lines: list[str] = []
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
        return [
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
        self.state.service_state = "starting"
        self.state.status_line = "service restarting after crash"
        self.ui.render(
            self.ui.print_header()
            + self.ui.panel(
                "Service Restart",
                ["Existing Tele-Cli instance detected.", "", "Restarting the local background service now."],
                align="center",
            )
        )
        print()
        self.ui.spinner("Stopping previous instance", 0.65)
        self.ui.spinner("Starting Tele-Cli", 0.75)
        self.state.service_state = "running"
        self.state.status_line = "waiting for Telegram commands"

    def _debug_screen(self) -> None:
        frames = [
            [
                f"{Colors.blue}$ codex{Colors.reset}",
                f"{Colors.cyan}> Thinking...{Colors.reset}",
                "> Reviewing Telegram command stream",
                "> Ready for operator input",
            ],
            [
                f"{Colors.blue}$ codex{Colors.reset}",
                f"{Colors.cyan}> Thinking...{Colors.reset}",
                "> Building response draft",
                f"{Colors.green}> Waiting for next prompt{Colors.reset}",
            ],
        ]
        frame_index = 0
        while True:
            self.ui.render(
                self.ui.print_header()
                + self.ui.panel(
                    "Debug Mode",
                    [f"{Colors.dim}Displaying the Codex terminal session.{Colors.reset}", "", *frames[frame_index % len(frames)]],
                    width=76,
                )
            )
            frame_index += 1
            key = self.ui.timed_keypress(0.85)
            if key in {"q", "esc"}:
                self.state.status_line = "returned from debug mode"
                return

    def _update_screen(self) -> None:
        self.ui.render(
            self.ui.print_header()
            + self.ui.panel("Updating Tele-Cli", ["Preparing package refresh and service restart."], align="center")
        )
        print()
        for step in [
            "Installing dependencies",
            "Installing Codex",
            "Creating configuration",
            "Starting Tele-Cli service",
        ]:
            self.ui.spinner(step, 0.6)
        self.state.status_line = "Tele-Cli updated successfully"

    def _uninstall_screen(self) -> None:
        self.ui.render(
            self.ui.print_header()
            + self.ui.panel(
                "Uninstall",
                [
                    f"{Colors.red}{Colors.bold}You are about to remove Tele-Cli.{Colors.reset}",
                    "",
                    "This will delete:",
                    f"{Colors.dim}~/.tele-cli{Colors.reset}",
                    f"{Colors.dim}Tele-Cli service{Colors.reset}",
                    f"{Colors.dim}tele-cli command{Colors.reset}",
                ],
                width=74,
            )
        )
        confirmation = self.ui.input_line("Type REMOVE to continue", panel_width=74)
        if confirmation != "REMOVE":
            self.state.status_line = "Uninstall cancelled"
            return

        self.ui.render(self.ui.print_header() + self.ui.panel("Uninstall", ["Removing Tele-Cli..."], align="center"))
        print()
        self.ui.spinner("Stopping Tele-Cli service", 0.55)
        self.ui.spinner("Removing files", 0.55)
        self.ui.spinner("Removing command", 0.55)
        self.ui.render(
            self.ui.print_header()
            + self.ui.panel("Uninstall Complete", [f"{Colors.green}{Colors.bold}Tele-Cli successfully removed.{Colors.reset}"], align="center")
        )
        self.ui.pause("Press Enter to exit the demo.")
        self.state.running = False

    def _state_chip(self, value: str) -> str:
        normalized = value.upper()
        if normalized in {"RUNNING", "AUTHENTICATED", "PAIRED"}:
            color = Colors.chip_on
        elif normalized in {"STARTING", "CONNECTING", "INSTALLING"}:
            color = Colors.chip_warn
        else:
            color = Colors.chip_off
        return f"{color} {normalized} {Colors.reset}"

    def _is_valid_token(self, token: str) -> bool:
        return token.strip().lower() != "x"
