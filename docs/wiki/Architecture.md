# Architecture

## Modules

- `cli.py`: command entrypoint
- `setup_flow.py`: first-run setup and recovery
- `service.py`: service orchestration
- `installer.py`: package-manager-specific installation strategy
- `telegram.py`: Telegram API and pairing model
- `codex_runtime.py`: Codex subprocess wrapper
- `process.py`: PID and ownership inspection
- `locks.py`: lock metadata persistence
- `recorder.py`: replayable terminal log
- `debug_mirror.py`: local debug output mirror

## Runtime Model

- one active service instance
- one active Codex child
- one Telegram poller
- one recorder
- one debug mirror
