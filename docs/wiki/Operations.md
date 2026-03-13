# Operations

## Default State Directory

`~/.minic`

Contents:

- `app.lock`
- `setup.lock`
- `runtime.json`
- `auth.json`
- `config.json`
- `recovery.log`
- `terminal.log`

## Recovery Behavior

`minic` prompts before taking recovery actions when it finds:

- live conflicting instances
- stale runtime locks
- interrupted setup
- orphaned Codex child processes

Recovery decisions are appended to `recovery.log`.
