#!/usr/bin/env bash
set -euo pipefail

REPO_URL="https://github.com/Gazman-Dev/tele-cli.git"
PROJECT_NAME="Tele Cli"
STATE_DIR="${HOME}/.tele-cli"
USER_BIN_DIR="${HOME}/.local/bin"
SERVICE_NAME="tele-cli"
LAUNCHD_LABEL="dev.gazman.tele-cli"

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

ensure_dir() {
  mkdir -p "$1"
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

install_homebrew() {
  echo "Homebrew not found. Installing Homebrew..."
  NONINTERACTIVE=1 /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

  if [ -x /opt/homebrew/bin/brew ]; then
    eval "$(/opt/homebrew/bin/brew shellenv)"
  elif [ -x /usr/local/bin/brew ]; then
    eval "$(/usr/local/bin/brew shellenv)"
  fi
}

ensure_macos_prereqs() {
  if [ "$(uname -s)" != "Darwin" ]; then
    return
  fi

  if ! command -v npm >/dev/null 2>&1 && ! command -v brew >/dev/null 2>&1; then
    install_homebrew
  fi
}

append_path_hint() {
  local shell_rc=""
  local candidate

  if [ -n "${ZSH_VERSION:-}" ] || [ "${SHELL:-}" = "/bin/zsh" ]; then
    for candidate in "${HOME}/.zshrc" "${HOME}/.zprofile"; do
      if [ -e "$candidate" ]; then
        if [ -w "$candidate" ]; then
          shell_rc="$candidate"
          break
        fi
      elif [ -w "${HOME}" ]; then
        shell_rc="$candidate"
        break
      fi
    done
  else
    for candidate in "${HOME}/.bashrc" "${HOME}/.bash_profile" "${HOME}/.profile"; do
      if [ -e "$candidate" ]; then
        if [ -w "$candidate" ]; then
          shell_rc="$candidate"
          break
        fi
      elif [ -w "${HOME}" ]; then
        shell_rc="$candidate"
        break
      fi
    done
  fi

  ensure_dir "$USER_BIN_DIR"
  if ! printf '%s\n' "${PATH}" | tr ':' '\n' | grep -Fx "$USER_BIN_DIR" >/dev/null 2>&1; then
    if [ -n "$shell_rc" ]; then
      if [ -f "$shell_rc" ]; then
        if ! grep -F 'export PATH="$HOME/.local/bin:$PATH"' "$shell_rc" >/dev/null 2>&1; then
          printf '\nexport PATH="$HOME/.local/bin:$PATH"\n' >> "$shell_rc"
        fi
      else
        printf 'export PATH="$HOME/.local/bin:$PATH"\n' > "$shell_rc"
      fi
    else
      echo "Warning: could not find a writable shell rc file to persist PATH." >&2
    fi
    export PATH="$USER_BIN_DIR:$PATH"
  fi
}

write_launcher() {
  ensure_dir "$USER_BIN_DIR"
  cat > "${USER_BIN_DIR}/tele-cli" <<EOF
#!/usr/bin/env bash
exec "$PYTHON_BIN" -m minic.cli "\$@"
EOF
  chmod +x "${USER_BIN_DIR}/tele-cli"
  cp "${USER_BIN_DIR}/tele-cli" "${USER_BIN_DIR}/minic"
}

is_configured() {
  [ -f "${STATE_DIR}/config.json" ] && [ -f "${STATE_DIR}/auth.json" ]
}

install_or_upgrade_package() {
  echo "Installing ${PROJECT_NAME} from ${REPO_URL}"
  "$PYTHON_BIN" -m pip install --upgrade --force-reinstall --no-cache-dir "git+$REPO_URL"
}

run_setup_if_needed() {
  if is_configured; then
    echo "${PROJECT_NAME} is already configured. Skipping setup."
    return
  fi

  echo
  echo "Starting ${PROJECT_NAME} setup..."
  "$PYTHON_BIN" -m minic.cli setup
}

install_launchd_service() {
  local plist_dir plist_path
  plist_dir="${HOME}/Library/LaunchAgents"
  plist_path="${plist_dir}/${LAUNCHD_LABEL}.plist"
  ensure_dir "$plist_dir"
  ensure_dir "$STATE_DIR"

  cat > "$plist_path" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
  <dict>
    <key>Label</key>
    <string>${LAUNCHD_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
      <string>${PYTHON_BIN}</string>
      <string>-m</string>
      <string>minic.cli</string>
      <string>service</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>WorkingDirectory</key>
    <string>${HOME}</string>
    <key>StandardOutPath</key>
    <string>${STATE_DIR}/service.stdout.log</string>
    <key>StandardErrorPath</key>
    <string>${STATE_DIR}/service.stderr.log</string>
    <key>EnvironmentVariables</key>
    <dict>
      <key>PATH</key>
      <string>${PATH}</string>
    </dict>
  </dict>
</plist>
EOF

  launchctl bootout "gui/$(id -u)" "$plist_path" >/dev/null 2>&1 || true
  launchctl bootstrap "gui/$(id -u)" "$plist_path"
  launchctl enable "gui/$(id -u)/${LAUNCHD_LABEL}" >/dev/null 2>&1 || true
  launchctl kickstart -k "gui/$(id -u)/${LAUNCHD_LABEL}"
}

install_systemd_user_service() {
  local unit_dir unit_path
  unit_dir="${HOME}/.config/systemd/user"
  unit_path="${unit_dir}/${SERVICE_NAME}.service"
  ensure_dir "$unit_dir"
  ensure_dir "$STATE_DIR"

  cat > "$unit_path" <<EOF
[Unit]
Description=Tele Cli service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=${PYTHON_BIN} -m minic.cli service
WorkingDirectory=${HOME}
Restart=always
RestartSec=5
Environment=PATH=${PATH}

[Install]
WantedBy=default.target
EOF

  systemctl --user daemon-reload
  systemctl --user enable "${SERVICE_NAME}.service" >/dev/null 2>&1 || true
  systemctl --user restart "${SERVICE_NAME}.service" || systemctl --user start "${SERVICE_NAME}.service"
}

install_fallback_service() {
  local runner pid_file
  runner="${STATE_DIR}/run-service.sh"
  pid_file="${STATE_DIR}/service.pid"
  ensure_dir "$STATE_DIR"

  cat > "$runner" <<EOF
#!/usr/bin/env bash
exec "${PYTHON_BIN}" -m minic.cli service >> "${STATE_DIR}/service.stdout.log" 2>> "${STATE_DIR}/service.stderr.log"
EOF
  chmod +x "$runner"

  if [ -f "$pid_file" ] && kill -0 "$(cat "$pid_file")" >/dev/null 2>&1; then
    kill "$(cat "$pid_file")" >/dev/null 2>&1 || true
    sleep 1
  fi

  nohup "$runner" >/dev/null 2>&1 &
  echo $! > "$pid_file"
}

install_and_start_service() {
  case "$(uname -s)" in
    Darwin)
      install_launchd_service
      ;;
    Linux)
      if command -v systemctl >/dev/null 2>&1; then
        install_systemd_user_service
      else
        install_fallback_service
      fi
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
  echo "Python 3.9+ is required." >&2
  exit 1
fi

need_cmd "$PYTHON_BIN"

if ! "$PYTHON_BIN" - <<'EOF' >/dev/null 2>&1
import sys
sys.exit(0 if sys.version_info >= (3, 9) else 1)
EOF
then
  echo "Python 3.9+ is required." >&2
  exit 1
fi

if ! "$PYTHON_BIN" -m pip --version >/dev/null 2>&1; then
  echo "pip is required for installation." >&2
  exit 1
fi

if ! command -v git >/dev/null 2>&1; then
  install_git
fi

ensure_macos_prereqs
append_path_hint
install_or_upgrade_package
write_launcher
run_setup_if_needed
install_and_start_service

echo
echo "Install complete."
echo "Background service is installed and started."
echo "Launcher path: ${USER_BIN_DIR}/tele-cli"
