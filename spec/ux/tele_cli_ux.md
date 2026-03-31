# Tele Cli UX Spec

## Status

- Status: active working spec
- Date: March 31, 2026
- Scope: user-facing behavior across Telegram, local shell, and local chat

## Goal

Tele Cli should feel like one personal assistant that happens to expose multiple surfaces, not like separate tools glued together.

The operator should always understand:

- where to talk to it
- what state it is in
- how to recover when something is broken

## Product Identity

Tele Cli is:

- local-first
- single-operator
- Telegram-first for normal use
- app-shell-first for setup and maintenance
- direct and low-friction

It is not a generic bot for multiple people.

## User Surfaces

### Telegram

Telegram is the primary day-to-day interface.

It should support:

- normal free-form user messages
- short operational commands
- attachments
- short status and recovery replies

Final replies sent to Telegram should prefer simple valid MarkdownV2 when formatting helps readability.

### Interactive app shell

The app shell is the primary local control surface.

It should own:

- first-run setup
- token entry and pairing
- update and reinstall flows
- uninstall confirmation
- duplicate service and stale lock recovery
- high-level health and repair actions

### Local chat

Local chat is a direct on-device conversation path that reuses the same session model without depending on Telegram.

### Scriptable outbound Telegram sends

The CLI should support proactive outbound messages, images, and files to:

- `main`
- `current`
- an explicit chat id
- an explicit chat/topic pair

## Interaction Principles

### Keep normal replies short

Telegram is a messaging surface. Replies should usually be concise unless the task needs detail.

### Make system state legible

When Tele Cli talks about setup, login, degraded mode, or recovery, it should say what it found and what the operator can do next.

### Preserve continuity

If a session is still the active conversation for a chat or topic, follow-up messages should continue that session unless the operator explicitly starts a new one.

### Do not hide repair actions

If the product cannot safely resume a turn or reclaim ownership, the operator should see that explicitly.

## Telegram Command Contract

Tele Cli should support these baseline commands in Telegram:

- `/status`
- `/sessions`
- `/new`
- `/stop`
- `/abort`
- `/model <name>`
- `/reasoning <minimal|low|medium|high|xhigh>`
- `/approve <request_id>`
- `/deny <request_id>`

These commands should be short, predictable, and safe to use from a mobile chat.

## Session UX

### Telegram sessions

For each authorized chat or topic:

- one attached session is considered current
- `/new` detaches the previous session and creates a fresh attached one
- detached sessions may finish background work, but new unsuffixed user input routes to the current attached session

### Local sessions

Local chat and outbound session commands should use the same session naming idea:

- `main`
- `current`
- named or implicit local channels as needed by the CLI

### Session history visibility

The operator should be able to inspect recent sessions without manually dealing with raw Codex thread ids.

## App Shell UX Contract

The app shell should open for interactive runs of:

- `tele-cli`
- `tele-cli menu`
- `tele-cli setup`
- `tele-cli update`
- `tele-cli uninstall`

The shell should present:

- a splash or startup frame
- a status screen with service, Codex, and Telegram state
- clear next actions
- setup and repair flows when required

## Setup UX Contract

Interactive setup should happen inside the app shell.

Required steps:

1. dependency readiness
2. Telegram bot token entry and validation
3. Telegram pairing
4. service readiness confirmation

Raw shell prompts are acceptable only for narrow non-interactive fallback paths.

## Status UX Contract

The home status view should expose at least:

- service state
- Codex state
- Telegram state
- a one-line summary
- important details such as pairing state, dependency state, and login-required state

The operator should not need to inspect JSON files to answer basic health questions.

## Failure UX Contract

When something fails, Tele Cli should prefer messages like:

- what failed
- whether Telegram is still reachable
- whether recovery is automatic or needs the operator
- what command or action to take next

Examples of user-visible degraded states:

- Telegram token missing
- Telegram not paired
- Codex login required
- duplicate service registration detected
- stale lock detected
- session resume failed

## Non-Interactive Behavior

If no TTY is available, interactive shell behavior should not be forced.

Non-interactive flows may:

- print concise status
- run direct setup, update, or uninstall subcommands
- return machine-usable errors

## UX Success Criteria

The UX is correct when:

- normal Telegram use feels lightweight
- the local shell is the obvious place for setup and repair
- sessions behave consistently across Telegram and local chat
- failures are understandable without reading code
