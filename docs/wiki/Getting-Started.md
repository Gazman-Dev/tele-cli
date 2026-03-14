# Getting Started

## Install

```bash
python -m pip install -e .
```

Or:

```bash
curl -fsSL https://github.com/Gazman-Dev/tele-cli/raw/master/setup.sh | bash
```

That setup script will install or update the app, run setup if needed, register a background service that is restarted on later setup runs, and offer uninstall if it detects an existing install.

If the app is already installed, the script will prompt you to:

- press Enter to update
- type `uninstall` to remove it

Uninstall requires typing `uninstall` a second time for confirmation. It removes the service, launcher scripts, installed package, and the default state directory at `~/.tele-cli`.

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
