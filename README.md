# Tele Cli

`Tele Cli` is a single-operator terminal bridge for Codex and Telegram.

It is designed for Linux and macOS and focuses on one local operator, one Telegram bot, one authorized Telegram chat, one Codex session, and explicit recovery behavior when stale or conflicting runtime state is detected.

## Features

- Guided first-run setup for npm, Codex, and Telegram bot validation
- Separate setup and service conflict handling with recovery prompts
- Single-instance protection with runtime metadata and stale lock detection
- Single-controller Telegram pairing with local `reset-auth` support
- Terminal recording and local debug mirroring
- Docker-based Linux validation harness

## Project Layout

```text
src/minic/
  cli.py
  setup_flow.py
  service.py
  installer.py
  telegram.py
  codex_runtime.py
  process.py
  locks.py
  models.py
tests/
scripts/
Dockerfile.linux-test
```

## Requirements

- Python 3.11+
- Linux or macOS for actual runtime use
- Docker Desktop or Docker Engine for Linux container validation

## Install

```bash
python -m pip install -e .
```

One-line setup:

```bash
curl -fsSL https://raw.github.com/Gazman-Dev/tele-cli/refs/heads/master/install.sh | bash
```

The setup script now:

- installs or updates Tele Cli
- runs setup immediately if the app is not configured yet
- installs a managed background service
- starts or restarts that service on every install run
- offers uninstall when it detects an existing install
- avoids duplicate service ownership through the service manager and runtime lock checks

If `Tele Cli` is already installed, running `setup.sh` again will prompt you to either:

- press Enter to update the existing install
- type `uninstall` to start removal

Uninstall requires a second confirmation where you must type `uninstall` again. It removes the background service, launcher scripts, installed package, and the default state directory at `~/.tele-cli`.

## Commands

```bash
tele-cli setup
tele-cli service
tele-cli debug
tele-cli reset-auth
```

`minic` remains available as a compatibility alias, but `tele-cli` is the primary command.

Use `--state-dir` if you want state files somewhere other than `~/.tele-cli`.

## Docker Linux Test

```bash
./scripts/run_docker_tests.sh
```

PowerShell:

```powershell
powershell -File scripts/run_docker_tests.ps1
```

## State Files

`Tele Cli` stores runtime state under `~/.tele-cli` by default:

- `app.lock`
- `setup.lock`
- `runtime.json`
- `auth.json`
- `config.json`
- `recovery.log`
- `terminal.log`

## Debug Mode

Run:

```bash
tele-cli debug
```

`debug` runs the service in the foreground and keeps the local debug mirror on your terminal. If another Tele Cli instance already owns the runtime, the app will prompt with `kill`, `ignore`, or `exit`. Use `kill` to terminate the other owned instance and let the current debug run take over.

## License

Apache License 2.0, copyright Gazman Dev LLC.
