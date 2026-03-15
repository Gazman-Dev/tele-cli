# Getting Started

## Install

```bash
python -m pip install -e .
```

Or:

```bash
curl -fsSL https://github.com/Gazman-Dev/tele-cli/raw/master/setup.sh | bash
```

That setup script now bootstraps only enough to launch `tele-cli` for interactive installs and reinstalls, so setup, update, repair, and uninstall stay inside the app shell. Non-interactive runs keep the narrower fallback path that completes setup and service registration directly.

## First Run

```bash
tele-cli
tele-cli setup
tele-cli service
tele-cli update
tele-cli uninstall
```

## Reset Telegram Pairing

```bash
tele-cli reset-auth
```

## Linux Validation In Docker

```bash
./scripts/run_docker_tests.sh
```
