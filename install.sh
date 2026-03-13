#!/usr/bin/env bash
set -euo pipefail

REPO_URL="https://github.com/Gazman-Dev/tele-cli.git"

detect_package_manager() {
  if command -v apt >/dev/null 2>&1; then
    echo "apt"
    return
  fi
  if command -v dnf >/dev/null 2>&1; then
    echo "dnf"
    return
  fi
  if command -v yum >/dev/null 2>&1; then
    echo "yum"
    return
  fi
  if command -v pacman >/dev/null 2>&1; then
    echo "pacman"
    return
  fi
  if command -v zypper >/dev/null 2>&1; then
    echo "zypper"
    return
  fi
  if command -v brew >/dev/null 2>&1; then
    echo "brew"
    return
  fi
  echo ""
}

need_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1" >&2
    exit 1
  fi
}

install_git() {
  local pm
  pm="$(detect_package_manager)"

  if [ -z "$pm" ]; then
    echo "git is required, but no supported package manager was found." >&2
    exit 1
  fi

  echo "git not found. Installing via $pm"
  case "$pm" in
    apt)
      sudo apt update
      sudo apt install -y git
      ;;
    dnf)
      sudo dnf install -y git
      ;;
    yum)
      sudo yum install -y git
      ;;
    pacman)
      sudo pacman -S --noconfirm git
      ;;
    zypper)
      sudo zypper --non-interactive install git
      ;;
    brew)
      brew install git
      ;;
    *)
      echo "Unsupported package manager: $pm" >&2
      exit 1
      ;;
  esac
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

if ! command -v git >/dev/null 2>&1; then
  install_git
fi

echo "Installing Tele Cli from $REPO_URL"
"$PYTHON_BIN" -m pip install --upgrade "git+$REPO_URL"

echo
echo "Install complete."
echo "Next steps:"
echo "  tele-cli setup"
echo "  tele-cli service"
