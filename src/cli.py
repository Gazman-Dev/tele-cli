from __future__ import annotations

import argparse
import platform

from app_shell import run_app_shell
from core.json_store import load_json
from core.models import AuthState
from core.paths import build_paths
from integrations.telegram import TelegramClient
from runtime.service import reset_auth, run_service
from setup.admin import run_uninstall, run_update
from setup.setup_flow import complete_pending_pairing, run_setup


def _is_interactive_terminal() -> bool:
    try:
        import sys

        return sys.stdin.isatty() and sys.stdout.isatty()
    except Exception:
        return False


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tele-cli")
    parser.add_argument("--state-dir", default=None)
    subparsers = parser.add_subparsers(dest="command", required=False)
    subparsers.add_parser("menu")
    subparsers.add_parser("setup")
    subparsers.add_parser("service")
    subparsers.add_parser("reset-auth")
    subparsers.add_parser("update")
    subparsers.add_parser("uninstall")
    subparsers.add_parser("complete-pairing")
    return parser


def main() -> None:
    if platform.system() not in {"Linux", "Darwin"}:
        raise SystemExit("V1 supports Linux and macOS only.")
    parser = build_parser()
    args = parser.parse_args()
    paths = build_paths(args.state_dir)
    if args.command in {None, "menu"}:
        run_app_shell(paths)
    elif args.command == "setup":
        if _is_interactive_terminal():
            run_app_shell(paths, startup_action="setup")
        else:
            run_setup(paths)
    elif args.command == "service":
        run_service(paths)
    elif args.command == "reset-auth":
        reset_auth(paths)
    elif args.command == "update":
        if _is_interactive_terminal():
            run_app_shell(paths, startup_action="update")
        else:
            run_update(paths)
    elif args.command == "uninstall":
        if _is_interactive_terminal():
            run_app_shell(paths, startup_action="uninstall")
        else:
            run_uninstall(paths)
    elif args.command == "complete-pairing":
        auth = load_json(paths.auth, AuthState.from_dict)
        if not auth or not auth.bot_token:
            raise SystemExit("Telegram bot token is not configured.")
        complete_pending_pairing(paths, auth, TelegramClient(auth.bot_token))


if __name__ == "__main__":
    main()
