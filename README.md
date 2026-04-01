# Tele Cli

`Tele Cli` is a single-operator terminal bridge for Codex and Telegram.

It is designed for macOS and Linux and centers on one local operator, one Telegram bot, one authorized Telegram identity, one managed background service, and explicit recovery when setup or runtime state becomes stale or conflicting.

## Current Capabilities

- Interactive full-screen app shell for setup, update, repair, restart, uninstall, and status
- One-line installer that bootstraps Tele Cli and launches the same shell-driven setup flow
- Managed background service lifecycle with lock recovery and stale-process healing
- Telegram pairing flow with chat-delivered pairing codes and in-shell confirmation
- Telegram bot control surface for sessions, approvals, model selection, reasoning level, and turn control
- Telegram outbound channel commands for scripts and services to send text, images, and files
- Telegram inbound attachment intake for photos and documents
- Codex App Server integration with durable thread/session routing
- Root workspace plus per-topic workspaces with deterministic `cwd` routing for Codex turns
- Scaffolded root and topic `AGENTS.md` files plus committed `workspace/long_memory.md`
- Topic workspaces stored as independent Git repos with parent root linkage metadata
- Sleep flow that updates durable root memory and commits it into workspace Git history
- Clearer user-visible Codex failure messages, including auth-required and quota errors
- Telegram queue pause and retry behavior for delivery rate limits
- Local terminal recording, debug mirroring, performance logging, and runtime event storage
- Docker-based Linux validation harness plus unittest coverage for setup, service, Telegram, Codex app-server, storage, and UI flows

## Requirements

- Python `3.9+`
- macOS or Linux for normal runtime use
- A Telegram bot token
- Codex CLI/App Server available on the target machine
- Docker Desktop or Docker Engine if you want to run the Linux validation harness

## Install

Editable install:

```bash
python -m pip install -e .
```

One-line install:

```bash
curl -fsSL https://raw.githubusercontent.com/Gazman-Dev/tele-cli/master/install.sh | bash
```

The public installer fetches `setup.sh` with cache-busting query parameters, installs or updates Tele Cli, then launches the interactive app shell. If Tele Cli is already installed, running the installer again returns to the same shell-driven update and repair flow instead of branching into a different path.

## Main Commands

```bash
tele-cli
tele-cli menu
tele-cli setup
tele-cli service
tele-cli update
tele-cli uninstall
tele-cli reset-auth
tele-cli complete-pairing
tele-cli telegram channel message --channel main "hello"
tele-cli telegram channel image --channel current ./image.png --caption "preview"
tele-cli telegram channel file --channel -100123456/77 ./report.pdf --caption "report"
```

`tele-cli` and `tele-cli menu` open the interactive shell. In an interactive terminal, `setup`, `update`, and `uninstall` route back through that shell so the UX stays consistent. Direct non-interactive execution remains available when no TTY is present.

## Telegram Chat Commands

Once the bot is paired, the authorized Telegram chat can use:

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

If Codex authentication is required, Tele Cli now sends the login URL back to Telegram and accepts the localhost callback URL pasted into chat to complete sign-in. Manual device login on the host is also still supported.

## Workspaces And Memory

Tele Cli keeps durable operator context under a workspace tree inside the state directory:

```text
~/.tele-cli/
  workspace/
    AGENTS.md
    long_memory.md
    topics/
      <topic>/
        AGENTS.md
```

- `workspace/` is the root workspace for the direct 1:1 operator chat
- `workspace/topics/<topic>/` is the dedicated workspace for one Telegram topic
- each topic workspace has its own Git repository
- `workspace/long_memory.md` is durable committed memory managed by Tele Cli
- `AGENTS.md` files are scaffolded defaults for Codex-native workspace guidance
- temporary lessons and short-memory files stay under `memory/` and are not treated as workspace durable memory

## Telegram Channels

For outbound scriptable Telegram sends:

```bash
tele-cli telegram channel message --channel current "Done."
tele-cli telegram channel message --channel main "Done."
tele-cli telegram channel image --channel current ./image.png --caption "Preview"
tele-cli telegram channel file --channel -100123456/77 ./report.pdf --caption "Report"
```

Channel formats:

- `main`: the default paired chat
- `current`: the most recently active attached Telegram session or topic
- `<chat_id>`: an explicit Telegram chat id
- `<chat_id>/<topic_id>`: an explicit Telegram topic inside a group chat

## State Layout

Tele Cli stores state under `~/.tele-cli` by default. The exact contents evolve over time, but the main files and directories include:

- `auth.json`
- `config.json`
- `tele_cli.db`
- `app.lock`
- `setup.lock`
- `terminal.log`
- `performance.log`
- `workspace/`
- `memory/`
- `system/`

The SQLite database now holds runtime state, sessions, events, approvals, Telegram queue data, and workspace metadata.

## Codex Runtime Defaults

By default, Tele Cli starts Codex App Server threads in full-access mode through `config.json`:

- `sandbox_mode = "danger-full-access"`
- `approval_policy = "never"`

Override those values in `config.json` if you want stricter behavior.

## UX Demo

To review the mock CLI UX before or alongside production changes:

```bash
python ux_demo.py
```

If installed as a package:

```bash
tele-cli-ux-demo
```

The demo mirrors the setup, dashboard, update, and uninstall flows without touching the real service or Telegram integration.

## Tests

Run the unittest suite:

```bash
python -m unittest discover -s tests -p "test_*.py"
```

Run the Linux Docker validation harness:

```bash
./scripts/run_docker_tests.sh
```

PowerShell:

```powershell
powershell -File scripts/run_docker_tests.ps1
```

## Specs

The main design docs live under [`spec/`](spec/README.md), including:

- Codex app-server integration
- services and lifecycle behavior
- workspace and topic memory
- UI migration plans
- storage layout and SQLite migration notes

## License

Apache License 2.0, copyright Gazman Dev LLC.
