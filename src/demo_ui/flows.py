from __future__ import annotations

import random
import time

from .state import Colors, DemoExit, DemoState
from .ui import TerminalUI


def run_token_screen(ui: TerminalUI, state: DemoState) -> None:
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

        ui.render(
            ui.print_header()
            + ui.panel("Telegram Bot Setup", lines, width=74)
            + ui.input_section("Paste bot token", 74, title="Bot Token")
        )
        token = ui.input_line("Paste bot token", panel_width=74, use_existing_field=True)
        if token.strip().lower() != "x":
            state.token = token
            state.status_line = "Telegram token saved"
            ui.render(
                ui.print_header()
                + ui.panel("Telegram Bot Setup", [f"{Colors.green}{Colors.bold}Token saved.{Colors.reset}"], align="center")
            )
            time.sleep(0.9)
            return
        error = "Invalid Telegram token. In this demo, only x fails."


def run_pairing_screen(ui: TerminalUI, state: DemoState) -> None:
    state.pairing_requested = False
    state.pairing_code = ""
    error = ""
    while True:
        lines = [
            "Pair this machine with your Telegram user.",
            "",
            "1. Send any message to your bot.",
            "2. The bot replies with your unique code.",
            "3. Enter that code on this machine.",
        ]
        if not state.pairing_requested:
            lines.extend(
                [
                    "",
                    f"{Colors.muted}Waiting for the first Telegram message.{Colors.reset}",
                    "",
                    f"{Colors.muted}Demo: press m to simulate the bot reply.{Colors.reset}",
                ]
            )
            ui.render(ui.print_header() + ui.panel("Telegram Pairing", lines, width=74, align="center"))
            key = ui.read_key()
            if key == "m":
                state.pairing_requested = True
                state.pairing_code = f"{random.randint(0, 999999):06d}"
                state.status_line = "Telegram replied with a unique code"
            elif key == "esc":
                raise DemoExit(0)
            continue

        lines.extend(
            [
                "",
                f"{Colors.muted}Telegram replied with this code{Colors.reset}",
                f"{Colors.green}{Colors.bold}Telegram code{Colors.reset}",
                f"{Colors.chip_focus}   {state.pairing_code}   {Colors.reset}",
                "",
                f"{Colors.muted}Enter it below to confirm this machine.{Colors.reset}",
            ]
        )
        if error:
            lines.extend(["", f"{Colors.red}{error}{Colors.reset}"])

        ui.render(
            ui.print_header()
            + ui.panel("Telegram Pairing", lines, width=74, align="center")
            + ui.input_section("Type the Telegram code", 74, title="Pairing Code")
        )
        entered_code = ui.input_line("Type the Telegram code", panel_width=74, use_existing_field=True)
        if entered_code.lower() == "x":
            error = "Invalid pairing code. Copy the code from the Telegram bot reply."
            continue
        if entered_code != state.pairing_code:
            error = "Code mismatch. Enter the exact code sent by the Telegram bot."
            continue

        state.telegram_state = "paired"
        state.status_line = "Device successfully paired"
        ui.render(
            ui.print_header()
            + ui.panel("Telegram Pairing", [f"{Colors.green}{Colors.bold}Device successfully paired.{Colors.reset}"], align="center")
        )
        time.sleep(0.9)
        return


def show_setup_complete(ui: TerminalUI, state: DemoState) -> None:
    state.configured = True
    state.service_state = "running"
    state.codex_state = "authenticated"
    state.telegram_state = "paired"
    state.status_line = "waiting for Telegram commands"
    ui.render(
        ui.print_header()
        + ui.panel(
            "Setup Complete",
            [f"{Colors.green}{Colors.bold}Setup complete.{Colors.reset}", "", "Starting Tele-Cli service..."],
            align="center",
        )
    )
    time.sleep(1.0)


def show_service_restart(ui: TerminalUI, state: DemoState) -> None:
    state.service_state = "starting"
    state.status_line = "service restarting after crash"
    ui.render(
        ui.print_header()
        + ui.panel(
            "Service Restart",
            ["Existing Tele-Cli instance detected.", "", "Restarting the local background service now."],
            align="center",
        )
    )
    print()
    ui.spinner("Stopping previous instance", 0.65)
    ui.spinner("Starting Tele-Cli", 0.75)
    state.service_state = "running"
    state.status_line = "waiting for Telegram commands"


def show_update(ui: TerminalUI, state: DemoState) -> None:
    ui.render(
        ui.print_header()
        + ui.panel("Updating Tele-Cli", ["Preparing package refresh and service restart."], align="center")
    )
    print()
    for step in [
        "Installing dependencies",
        "Installing Codex",
        "Creating configuration",
        "Starting Tele-Cli service",
    ]:
        ui.spinner(step, 0.6)
    state.status_line = "Tele-Cli updated successfully"


def show_uninstall(ui: TerminalUI, state: DemoState) -> None:
    ui.render(
        ui.print_header()
        + ui.panel(
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
    confirmation = ui.input_line("Type REMOVE to continue", panel_width=74)
    if confirmation != "REMOVE":
        state.status_line = "Uninstall cancelled"
        return

    ui.render(ui.print_header() + ui.panel("Uninstall", ["Removing Tele-Cli..."], align="center"))
    print()
    ui.spinner("Stopping Tele-Cli service", 0.55)
    ui.spinner("Removing files", 0.55)
    ui.spinner("Removing command", 0.55)
    ui.render(
        ui.print_header()
        + ui.panel("Uninstall Complete", [f"{Colors.green}{Colors.bold}Tele-Cli successfully removed.{Colors.reset}"], align="center")
    )
    ui.pause("Press Enter to exit the demo.")
    state.running = False
