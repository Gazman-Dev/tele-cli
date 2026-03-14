# Tele Cli Wiki

`Tele Cli` is a single-operator Codex and Telegram bridge with explicit setup recovery and runtime ownership controls.

## Pages

- [Getting Started](Getting-Started)
- [Operations](Operations)
- [Architecture](Architecture)

## Setup Flow

- `setup.sh` is the recommended one-line entrypoint for install and update
- running `setup.sh` on an existing install offers uninstall with explicit typed confirmation

## Core Ideas

- One operator
- One Telegram bot
- One authorized chat
- One Codex session
- Explicit recovery instead of silent cleanup
