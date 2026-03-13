# Getting Started

## Install

```bash
python -m pip install -e .
```

Or:

```bash
curl -fsSL https://raw.githubusercontent.com/Gazman-Dev/tele-cli/master/install.sh | bash
```

That installer will install or update the app, run setup if needed, and register a background service that is restarted on later install runs.

## First Run

```bash
tele-cli setup
tele-cli service
```

## Reset Telegram Pairing

```bash
tele-cli reset-auth
```

## Linux Validation In Docker

```bash
./scripts/run_docker_tests.sh
```
