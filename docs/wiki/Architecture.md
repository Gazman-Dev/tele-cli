# Architecture

## Modules

- `cli.py`: top-level command routing
- `app_shell.py`: interactive app shell, setup flows, and maintenance actions
- `core/`: shared models, paths, locks, logging, process inspection, and persistence helpers
- `integrations/telegram.py`: Telegram API client and single-user pairing model
- `setup/`: dependency installation, setup recovery, service registration, update, and uninstall flows
- `runtime/`: long-running service, Codex app-server integration, session tracking, approvals, recorder, and performance logging
- `demo_ui/`: mock UX implementation for the TUI spec

## Runtime Model

- one active service instance
- one active Codex child
- one Telegram poller
- one recorder
- one runtime output mirror
- one or more persisted Telegram-backed conversation sessions, with one active session per chat or topic
