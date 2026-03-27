# Getting Started

## Install

```bash
python -m pip install -e .
```

Or:

```bash
curl -fsSL https://raw.githubusercontent.com/Gazman-Dev/tele-cli/master/install.sh | bash
```

That install wrapper fetches the latest `setup.sh` with a cache-busting URL, then bootstraps only enough to launch `tele-cli` for interactive installs and reinstalls. Setup, update, repair, and uninstall stay inside the app shell. Non-interactive runs keep the narrower fallback path that completes setup and service registration directly.

## First Run

```bash
tele-cli
tele-cli menu
tele-cli setup
tele-cli service
tele-cli update
tele-cli uninstall
tele-cli complete-pairing
```

`tele-cli` opens the interactive app shell. In a TTY, `setup`, `update`, and `uninstall` return to that shell and run the requested flow there. Direct non-interactive execution is still supported when no TTY is available.

## Reset Telegram Pairing

```bash
tele-cli reset-auth
```

## Telegram Chat Commands

After pairing, the authorized Telegram chat can use:

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

## Linux Validation In Docker

```bash
./scripts/run_docker_tests.sh
```
