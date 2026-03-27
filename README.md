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
  app_shell.py
  core/
  integrations/
  runtime/
  setup/
  demo_ui/
tests/
docs/
scripts/
Dockerfile.linux-test
```

## Requirements

- Python 3.9+
- Linux or macOS for actual runtime use
- Docker Desktop or Docker Engine for Linux container validation

## Install

```bash
python -m pip install -e .
```

One-line setup:

```bash
curl -fsSL https://raw.githubusercontent.com/Gazman-Dev/tele-cli/master/install.sh | bas
```

The setup script now:

- installs or updates Tele Cli
- bootstraps only enough to launch `tele-cli`
- opens the full-screen app shell for interactive installs and reinstalls
- keeps non-interactive setup/service installation as a fallback path
- avoids duplicate service ownership through the service manager and runtime lock checks

The public `install.sh` wrapper fetches `setup.sh` with cache-busting query parameters so installer updates are not blocked by stale raw-file caches.

If `Tele Cli` is already installed, running `setup.sh` again now relaunches the same app shell after bootstrap so update, repair, restart, or uninstall decisions stay in one place.

## Commands

```bash
tele-cli
tele-cli menu
tele-cli setup
tele-cli service
tele-cli update
tele-cli uninstall
tele-cli reset-auth
tele-cli complete-pairing
```

`tele-cli` and `tele-cli menu` open the interactive app shell. In an interactive terminal, `setup`, `update`, and `uninstall` also route through the app shell; the direct subcommands remain available for non-interactive use.

## Telegram Commands

Once the bot is paired, the authorized Telegram chat can use:

```text
/status
/sessions
/new
/stop
/abort
/model <name>
/reasoning <minimal|low|medium|high|xhigh>
/approve <request_id>
/deny <request_id>
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

The demo is a mock TUI for the UX spec in `spec/ux_spec.md`. It includes the setup screens, status dashboard, update flow, and uninstall confirmation, but it does not touch the real service or Telegram integration.

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
- `sessions.json`
- `telegram_updates.json`
- `approvals.json`
- `codex_server.json`
- `recovery.log`
- `terminal.log`
- `performance.log`
- `app_server_notifications.log`

## Codex Mode

Tele Cli now starts Codex App Server threads in full-access mode by default through `config.json`:

- `sandbox_mode = "danger-full-access"`
- `approval_policy = "never"`

That means Codex threads are created without local sandboxing and without approval prompts unless you override those values in `config.json`.

## License

Apache License 2.0, copyright Gazman Dev LLC.
