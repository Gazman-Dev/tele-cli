# Tele Cli

`Tele Cli` is a single-operator terminal bridge for Codex and Telegram.

It is designed for Linux and macOS and focuses on one local operator, one Telegram bot, one authorized Telegram chat, one Codex session, and explicit recovery behavior when stale or conflicting runtime state is detected.

## Features

- Guided first-run setup for npm, Codex, and Telegram bot validation
- Separate setup and service conflict handling with recovery prompts
- Single-instance protection with runtime metadata and stale lock detection
- Single-controller Telegram pairing with local `reset-auth` support
- Terminal recording and local runtime output mirroring
- Docker-based Linux validation harness

## Project Layout

```text
src/
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
- bootstraps only enough to launch `tele-cli`
- opens the full-screen app shell for interactive installs and reinstalls
- keeps non-interactive setup/service installation as a fallback path
- avoids duplicate service ownership through the service manager and runtime lock checks

If `Tele Cli` is already installed, running `setup.sh` again now relaunches the same app shell after bootstrap so update, repair, restart, or uninstall decisions stay in one place.

## Commands

```bash
tele-cli
tele-cli setup
tele-cli service
tele-cli update
tele-cli uninstall
tele-cli reset-auth
```

## UX Demo

To review the proposed CLI UX before implementing it in the production flow:

```bash
python ux_demo.py
```

If installed as a package:

```bash
tele-cli-ux-demo
```

The demo is a mock TUI for the UX spec in [`spec/ux_spec.md`](/C:/git/MiniC/spec/ux_spec.md). It includes the setup screens, status dashboard, update flow, and uninstall confirmation, but it does not touch the real service or Telegram integration.

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

## Codex Mode

Tele Cli now starts Codex App Server threads in full-access mode by default through `config.json`:

- `sandbox_mode = "danger-full-access"`
- `approval_policy = "never"`

That means Codex threads are created without local sandboxing and without approval prompts unless you override those values in `config.json`.

## License

Apache License 2.0, copyright Gazman Dev LLC.
