from __future__ import annotations

import argparse
import platform

from .paths import build_paths
from .service import reset_auth, run_service
from .setup_flow import run_setup


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tele-cli")
    parser.add_argument("--state-dir", default=None)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("setup")
    subparsers.add_parser("service")
    subparsers.add_parser("debug")
    subparsers.add_parser("reset-auth")
    return parser


def main() -> None:
    if platform.system() not in {"Linux", "Darwin"}:
        raise SystemExit("V1 supports Linux and macOS only.")
    parser = build_parser()
    args = parser.parse_args()
    paths = build_paths(args.state_dir)
    if args.command == "setup":
        run_setup(paths)
    elif args.command == "service":
        run_service(paths)
    elif args.command == "debug":
        run_service(paths)
    elif args.command == "reset-auth":
        reset_auth(paths)


if __name__ == "__main__":
    main()
