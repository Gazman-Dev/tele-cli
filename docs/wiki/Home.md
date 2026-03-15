# Tele Cli Wiki

`Tele Cli` is a single-operator Codex and Telegram bridge with explicit setup recovery and runtime ownership controls.

## Pages

- [Getting Started](Getting-Started)
- [Operations](Operations)
- [Architecture](Architecture)

## Setup Flow

- `install.sh` is the recommended one-line entrypoint for install and update
- `install.sh` fetches `setup.sh` with cache-busting query parameters to avoid stale raw-file caches
- interactive `setup.sh` runs now bootstrap into the unified `tele-cli` app shell instead of managing reinstall and uninstall in raw shell prompts

## Core Ideas

- One operator
- One Telegram bot
- One authorized chat
- One Codex session
- Explicit recovery instead of silent cleanup
