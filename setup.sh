#!/usr/bin/env bash
set -euo pipefail

REPO_URL="https://github.com/Gazman-Dev/tele-cli.git"
PROJECT_NAME="Tele Cli"
STATE_DIR="${HOME}/.tele-cli"
USER_BIN_DIR="${HOME}/.local/bin"
SERVICE_NAME="tele-cli"
LAUNCHD_LABEL="dev.gazman.tele-cli"
PACKAGE_SPEC="git+${REPO_URL}"

log() {
  printf '%s\n' "$1"
}

warn() {
  printf 'Warning: %s\n' "$1" >&2
}

prompt_input() {
  local message="$1"
  local answer_var="$2"
  local response=""

  if [ -r /dev/tty ]; then
    printf '%s' "$message" > /dev/tty
    IFS= read -r response < /dev/tty || response=""
  else
    warn "interactive input is unavailable; continuing with update."
  fi

  printf -v "$answer_var" '%s' "$response"
}

need_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    printf 'Missing required command: %s\n' "$1" >&2
    exit 1
  fi
}

ensure_dir() {
  mkdir -p "$1"
}

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

install_git() {
  local pm
  pm="$(detect_package_manager)"

  if [ -z "$pm" ]; then
    echo "git is required, but no supported package manager was found." >&2
    exit 1
  fi

  log "Installing git via ${pm}..."
  case "$pm" in
    apt)
      sudo apt update -qq
      sudo apt install -y -qq git
      ;;
    dnf)
      sudo dnf install -y -q git
      ;;
    yum)
      sudo yum install -y -q git
      ;;
    pacman)
      sudo pacman -S --noconfirm --needed git >/dev/null
      ;;
    zypper)
      sudo zypper --non-interactive --quiet install git
      ;;
    brew)
      HOMEBREW_NO_AUTO_UPDATE=1 brew install git >/dev/null
      ;;
    *)
      echo "Unsupported package manager: $pm" >&2
      exit 1
      ;;
  esac
}

install_homebrew() {
  log "Installing Homebrew..."
  NONINTERACTIVE=1 /bin/bash -c "$(curl -fsSL https://github.com/Homebrew/install/raw/HEAD/install.sh)" >/dev/null

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
      warn "could not find a writable shell rc file to persist PATH."
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
  "$PYTHON_BIN" - <<EOF
import json
from pathlib import Path

state_dir = Path("${STATE_DIR}")
config_path = state_dir / "config.json"
auth_path = state_dir / "auth.json"

if not config_path.exists() or not auth_path.exists():
    raise SystemExit(1)

config = json.loads(config_path.read_text())
auth = json.loads(auth_path.read_text())

if not auth.get("bot_token"):
    raise SystemExit(1)
if not auth.get("telegram_user_id") or not auth.get("telegram_chat_id"):
    raise SystemExit(1)

raise SystemExit(0)
EOF
}

is_installed() {
  if command -v tele-cli >/dev/null 2>&1; then
    return 0
  fi
  if [ -x "${USER_BIN_DIR}/tele-cli" ]; then
    return 0
  fi
  if [ -d "${STATE_DIR}" ]; then
    return 0
  fi
  return 1
}

prompt_existing_install_action() {
  local answer
  echo
  log "${PROJECT_NAME} appears to already be installed."
  prompt_input 'Press Enter to update it, or type uninstall to remove it: ' answer
  if [ "$answer" = "uninstall" ]; then
    confirm_uninstall
    uninstall_all
    exit 0
  fi
}

confirm_uninstall() {
  local confirmation
  prompt_input 'Type uninstall to confirm removal: ' confirmation
  if [ "$confirmation" != "uninstall" ]; then
    log "Uninstall cancelled."
    exit 1
  fi
}

install_or_upgrade_package() {
  log "Installing ${PROJECT_NAME}..."
  "$PYTHON_BIN" -m pip install --disable-pip-version-check --quiet --upgrade --force-reinstall --no-cache-dir "$PACKAGE_SPEC"
}

run_setup_if_needed() {
  if is_configured; then
    log "${PROJECT_NAME} is already configured."
    return
  fi

  echo
  log "Starting ${PROJECT_NAME} setup..."
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
  launchctl bootstrap "gui/$(id -u)" "$plist_path" >/dev/null
  launchctl enable "gui/$(id -u)/${LAUNCHD_LABEL}" >/dev/null 2>&1 || true
  launchctl kickstart -k "gui/$(id -u)/${LAUNCHD_LABEL}" >/dev/null
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

  systemctl --user daemon-reload >/dev/null 2>&1 || true
  systemctl --user enable "${SERVICE_NAME}.service" >/dev/null 2>&1 || true
  systemctl --user restart "${SERVICE_NAME}.service" >/dev/null 2>&1 || systemctl --user start "${SERVICE_NAME}.service" >/dev/null 2>&1
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
  log "Installing background service..."
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

remove_launchd_service() {
  local plist_path
  plist_path="${HOME}/Library/LaunchAgents/${LAUNCHD_LABEL}.plist"

  if [ -f "$plist_path" ]; then
    launchctl bootout "gui/$(id -u)" "$plist_path" >/dev/null 2>&1 || true
    rm -f "$plist_path"
  fi
}

remove_systemd_user_service() {
  local unit_path
  unit_path="${HOME}/.config/systemd/user/${SERVICE_NAME}.service"

  if command -v systemctl >/dev/null 2>&1; then
    systemctl --user stop "${SERVICE_NAME}.service" >/dev/null 2>&1 || true
    systemctl --user disable "${SERVICE_NAME}.service" >/dev/null 2>&1 || true
    systemctl --user daemon-reload >/dev/null 2>&1 || true
  fi

  rm -f "$unit_path"
}

remove_fallback_service() {
  local pid_file runner
  pid_file="${STATE_DIR}/service.pid"
  runner="${STATE_DIR}/run-service.sh"

  if [ -f "$pid_file" ]; then
    if kill -0 "$(cat "$pid_file")" >/dev/null 2>&1; then
      kill "$(cat "$pid_file")" >/dev/null 2>&1 || true
    fi
    rm -f "$pid_file"
  fi

  rm -f "$runner"
}

remove_service() {
  case "$(uname -s)" in
    Darwin)
      remove_launchd_service
      ;;
    Linux)
      remove_systemd_user_service
      remove_fallback_service
      ;;
  esac
}

uninstall_package() {
  if "$PYTHON_BIN" -m pip show tele-cli >/dev/null 2>&1; then
    "$PYTHON_BIN" -m pip uninstall -y tele-cli >/dev/null 2>&1 || true
    return
  fi

  if "$PYTHON_BIN" -m pip show minic >/dev/null 2>&1; then
    "$PYTHON_BIN" -m pip uninstall -y minic >/dev/null 2>&1 || true
  fi
}

remove_launchers() {
  rm -f "${USER_BIN_DIR}/tele-cli" "${USER_BIN_DIR}/minic"
}

remove_state() {
  rm -rf "${STATE_DIR}"
}

uninstall_all() {
  log "Removing ${PROJECT_NAME}..."
  remove_service
  uninstall_package
  remove_launchers
  remove_state
  echo
  log "Uninstall complete."
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

if is_installed; then
  prompt_existing_install_action
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
log "Setup complete."
log "Background service is installed and started."
log "Launcher path: ${USER_BIN_DIR}/tele-cli"
