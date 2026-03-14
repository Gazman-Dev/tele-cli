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

## Debug Mode

```bash
tele-cli debug
```

If another owned instance is already running, Tele Cli will prompt you to kill it so the current run can take over.

## Linux Validation In Docker

```bash
./scripts/run_docker_tests.sh
```
