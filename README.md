# minic

`minic` is a single-operator terminal bridge for Codex and Telegram.

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

## Commands

```bash
minic setup
minic service
minic reset-auth
```

Use `--state-dir` if you want state files somewhere other than `~/.minic`.

## Docker Linux Test

```bash
./scripts/run_docker_tests.sh
```

PowerShell:

```powershell
powershell -File scripts/run_docker_tests.ps1
```

## State Files

`minic` stores runtime state under `~/.minic` by default:

- `app.lock`
- `setup.lock`
- `runtime.json`
- `auth.json`
- `config.json`
- `recovery.log`
- `terminal.log`

## License

Apache License 2.0, copyright Gazman Dev LLC and Ilya Gazman.
