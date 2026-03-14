from __future__ import annotations

import json
import sys
from pathlib import Path

Colors = None


def build_frames() -> list[dict[str, object]]:
    repo_root = Path(__file__).resolve().parents[1]
    src_dir = repo_root / "src"
    if str(src_dir) not in sys.path:
        sys.path.insert(0, str(src_dir))

    from minic.ux_demo import Colors as DemoColors, TeleCliUxDemo

    global Colors
    Colors = DemoColors

    demo = TeleCliUxDemo()
    ui = demo.ui

    frames: list[dict[str, object]] = []

    splash_lines = ui.splash_frame(1)
    frames.append({"name": "00_splash", "lines": splash_lines})

    install_lines = (
        ui.print_header()
        + ui.system_strip("starting", "not authenticated", "not paired", "Preparing the local bridge and first-run setup.")
        + [""]
        + ui.panel(
            "Installing Tele-Cli",
            [
                f"{Colors.muted}Preparing local service, dependencies, and bridge wiring.{Colors.reset}",
                f"{Colors.green}*{Colors.reset} {Colors.text}Loading local background service{Colors.reset}",
                f"{Colors.green}*{Colors.reset} {Colors.text}Synchronizing AI engine{Colors.reset}",
                f"{Colors.green}*{Colors.reset} {Colors.text}Wiring Telegram API handlers{Colors.reset}",
                f"{Colors.green}*{Colors.reset} {Colors.text}Installing required Python packages{Colors.reset}",
            ],
            width=72,
            align="center",
        )
    )
    frames.append({"name": "01_install_complete", "lines": install_lines})

    demo.state.status_line = "Setup required"
    demo.state.service_state = "stopped"
    demo.state.codex_state = "not authenticated"
    demo.state.telegram_state = "not paired"
    menu_items = demo._menu_items()
    demo.selection = 1
    menu_lines = []
    for index, item in enumerate(menu_items):
        if index == demo.selection:
            menu_lines.append(f"{Colors.chip_focus} > {item.label.ljust(28)} {Colors.reset}")
        else:
            menu_lines.append(f"  {item.label.ljust(29)}")
    main_menu_lines = (
        ui.print_header()
        + ui.system_strip(
            demo.state.service_state,
            demo.state.codex_state,
            demo.state.telegram_state,
            demo.state.status_line,
        )
        + [""]
        + ui.panel("Menu", menu_lines, width=54)
    )
    frames.append({"name": "02_main_menu", "lines": main_menu_lines})

    token_lines = (
        ui.print_header()
        + ui.panel(
            "Telegram Bot Setup",
            [
                "Connect Telegram to this machine.",
                "",
                "Create a bot with BotFather and paste the token here.",
                "",
                f"{Colors.muted}BotFather: https://t.me/BotFather{Colors.reset}",
            ],
            width=74,
        )
        + ui.input_section("Paste bot token", 74, "123456:demo-token", "Bot Token")
    )
    frames.append({"name": "03_token_setup", "lines": token_lines})

    pairing_wait_lines = (
        ui.print_header()
        + ui.panel(
            "Telegram Pairing",
            [
                "Pair this machine with your Telegram user.",
                "",
                "1. Send any message to your bot.",
                "2. The bot replies with your unique code.",
                "3. Enter that code on this machine.",
                "",
                f"{Colors.muted}Waiting for the first Telegram message.{Colors.reset}",
                "",
                f"{Colors.muted}Demo: press m to simulate the bot reply.{Colors.reset}",
            ],
            width=74,
            align="center",
        )
        + ui.input_section("Type the Telegram code", 74, "", "Pairing Code")
    )
    frames.append({"name": "04_pairing_wait", "lines": pairing_wait_lines})

    code = "481203"
    pairing_confirm_lines = (
        ui.print_header()
        + ui.panel(
            "Telegram Pairing",
            [
                "Pair this machine with your Telegram user.",
                "",
                "1. Send any message to your bot.",
                "2. The bot replies with your unique code.",
                "3. Enter that code on this machine.",
                "",
                f"{Colors.muted}Telegram replied with this code{Colors.reset}",
                "",
                f"{Colors.green}{Colors.bold}Telegram code{Colors.reset}",
                f"{Colors.chip_focus}   {code}   {Colors.reset}",
                "",
                f"{Colors.muted}Enter it below to confirm this machine.{Colors.reset}",
            ],
            width=74,
            align="center",
        )
        + ui.input_section("Type the Telegram code", 74, code, "Pairing Code")
    )
    frames.append({"name": "05_pairing_confirm", "lines": pairing_confirm_lines})

    return frames
def main() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    output_dir = repo_root / "artifacts" / "ux_demo"
    output_dir.mkdir(parents=True, exist_ok=True)
    frames = build_frames()
    with (output_dir / "frames.json").open("w", encoding="utf-8") as handle:
        json.dump(frames, handle, ensure_ascii=False, indent=2)
    print(output_dir / "frames.json")


if __name__ == "__main__":
    main()
