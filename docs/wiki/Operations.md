# Operations

## Default State Directory

`~/.tele-cli`

The `setup.sh` uninstall flow removes this directory after explicit confirmation.

Contents:

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

## Recovery Behavior

`Tele Cli` prompts before taking recovery actions when it finds:

- live conflicting instances
- stale runtime locks
- interrupted setup
- orphaned Codex child processes

Recovery decisions are appended to `recovery.log`.

## Runtime Notes

- `config.json` defaults Codex to `sandbox_mode = "danger-full-access"` and `approval_policy = "never"` unless you override them
- `sessions.json` tracks Telegram conversation sessions and active Codex thread metadata
- `approvals.json` stores pending and stale approval requests
- `codex_server.json` stores Codex app-server auth and transport state
- `performance.log` records runtime timing data
- `app_server_notifications.log` records condensed Codex app-server notifications
