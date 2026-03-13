#!/usr/bin/env bash
set -euo pipefail

REPO_URL="https://github.com/Gazman-Dev/tele-cli.git"

need_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1" >&2
    exit 1
  fi
}

case "$(uname -s)" in
  Linux|Darwin)
    ;;
  *)
    echo "Tele Cli supports Linux and macOS only." >&2
    exit 1
    ;;
esac

if command -v python3 >/dev/null 2>&1; then
  PYTHON_BIN="python3"
elif command -v python >/dev/null 2>&1; then
  PYTHON_BIN="python"
else
  echo "Python 3.11+ is required." >&2
  exit 1
fi

need_cmd "$PYTHON_BIN"

if ! "$PYTHON_BIN" -m pip --version >/dev/null 2>&1; then
  echo "pip is required for installation." >&2
  exit 1
fi

echo "Installing Tele Cli from $REPO_URL"
"$PYTHON_BIN" -m pip install --upgrade "git+$REPO_URL"

echo
echo "Install complete."
echo "Next steps:"
echo "  tele-cli setup"
echo "  tele-cli service"
