from __future__ import annotations

import shutil
import sys
from dataclasses import dataclass

from .. import APP_VERSION
from ..core.json_store import load_json
from ..core.locks import LockFile
from ..core.models import AuthState, Config, RuntimeState, SetupState
from ..core.paths import AppPaths
from ..integrations.telegram import TelegramClient, describe_pairing, has_pending_pairing
from ..runtime.service import reset_auth, run_service
from ..setup.admin import run_uninstall, run_update
from ..setup.setup_flow import complete_pending_pairing, run_setup


@dataclass
class MenuItem:
    label: str
    action: str


def run_main_menu(paths: AppPaths) -> None:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise SystemExit("Interactive menu requires a TTY.")

    selection = 0
    while True:
        items = _build_menu_items(paths)
        selection %= len(items)
        _render(paths, items, selection)
        key = _read_key()
        if key == "up":
            selection = (selection - 1) % len(items)
        elif key == "down":
            selection = (selection + 1) % len(items)
        elif key == "enter":
            result = _run_action(paths, items[selection].action)
            if result == "exit":
                return
        elif key in {"q", "esc"}:
            return


def _build_menu_items(paths: AppPaths) -> list[MenuItem]:
    items = [
        MenuItem("Run setup", "setup"),
        MenuItem("Start service", "service"),
        MenuItem("Debug service", "debug"),
    ]
    auth = load_json(paths.auth, AuthState.from_dict)
    if auth and has_pending_pairing(auth):
        items.append(MenuItem("Complete Telegram pairing", "complete-pairing"))
    items.extend(
        [
            MenuItem("Reset Telegram auth", "reset-auth"),
            MenuItem("Update install", "update"),
            MenuItem("Uninstall", "uninstall"),
            MenuItem("Exit", "exit"),
        ]
    )
    return items


def _render(paths: AppPaths, items: list[MenuItem], selection: int) -> None:
    print("\033[2J\033[H", end="")
    print(f"Tele Cli {APP_VERSION}")
    print("Use Up/Down to move, Enter to select, q to exit.")
    print()
    for line in _build_status_lines(paths):
        print(line)
    print()
    print("Menu")
    for index, item in enumerate(items):
        marker = ">" if index == selection else " "
        print(f"{marker} {item.label}")


def _build_status_lines(paths: AppPaths) -> list[str]:
    setup = load_json(paths.setup_lock, SetupState.from_dict)
    auth = load_json(paths.auth, AuthState.from_dict)
    runtime = load_json(paths.runtime, RuntimeState.from_dict)
    config = load_json(paths.config, Config.from_dict)
    inspection = LockFile(paths.app_lock).inspect()

    npm_status = "installed" if shutil.which("npm") else "missing"
    codex_status = "installed" if shutil.which("codex") else "missing"
    token_status = "saved" if auth and auth.bot_token else "missing"

    service_status = "stopped"
    if inspection.exists and inspection.metadata:
        if inspection.live:
            service_status = f"running ({inspection.metadata.mode}) pid={inspection.metadata.pid}"
        else:
            service_status = f"stale lock pid={inspection.metadata.pid}"

    runtime_status = "no runtime data"
    if runtime:
        runtime_status = (
            f"service={runtime.service_state} telegram={runtime.telegram_state} "
            f"codex={runtime.codex_state}"
        )

    setup_status = setup.status if setup else "not started"
    state_dir = config.state_dir if config else str(paths.root)

    return [
        f"State dir: {state_dir}",
        f"Setup: {setup_status}",
        f"Service: {service_status}",
        f"Runtime: {runtime_status}",
        f"npm: {npm_status}",
        f"Codex: {codex_status}",
        f"Telegram token: {token_status}",
        f"Telegram pairing: {describe_pairing(auth)}",
    ]


def _run_action(paths: AppPaths, action: str) -> str | None:
    print("\033[2J\033[H", end="")
    if action == "setup":
        run_setup(paths)
    elif action == "service":
        run_service(paths)
    elif action == "debug":
        run_service(paths)
    elif action == "complete-pairing":
        auth = load_json(paths.auth, AuthState.from_dict)
        if not auth or not auth.bot_token:
            print("Telegram bot token is not configured.")
        else:
            completed = complete_pending_pairing(paths, auth, TelegramClient(auth.bot_token), allow_empty=True)
            if not completed:
                print("No pending pairing was completed.")
    elif action == "reset-auth":
        reset_auth(paths)
        print("Telegram auth reset.")
    elif action == "update":
        run_update()
    elif action == "uninstall":
        run_uninstall(paths)
        return "exit"
    elif action == "exit":
        return "exit"

    _pause()
    return None


def _pause() -> None:
    if sys.stdin.isatty():
        input("Press Enter to return to the menu...")


def _read_key() -> str:
    import termios
    import tty

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        first = sys.stdin.read(1)
        if first == "\x1b":
            second = sys.stdin.read(1)
            if second == "[":
                third = sys.stdin.read(1)
                if third == "A":
                    return "up"
                if third == "B":
                    return "down"
            return "esc"
        if first in {"\r", "\n"}:
            return "enter"
        return first.lower()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
