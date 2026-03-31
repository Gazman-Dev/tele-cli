# Tele Cli Product Spec

## Status

- Status: active working spec
- Date: March 31, 2026
- Scope: product definition for the current Tele Cli direction

## Product Summary

Tele Cli is a local, single-operator assistant that runs on the operator's own Linux or macOS machine.

It connects four surfaces:

- Telegram for day-to-day messaging
- a local interactive app shell for setup and maintenance
- a local chat/debug surface for direct use on the machine
- Codex App Server as the long-lived agent runtime

The product is intentionally optimized for one trusted operator, not for a shared or multi-tenant deployment.

## Primary Goal

The operator should be able to treat Tele Cli as a dependable personal assistant that:

- stays reachable from Telegram
- preserves session continuity across restarts when safe
- exposes a clear local control surface for setup, repair, update, and uninstall
- keeps enough local state and logs to explain what happened
- fails explicitly instead of silently deleting or replacing state

## Product Boundaries

### In scope

- one operator
- one bot token
- one authorized Telegram identity
- one paired main chat, with support for attached group topics and local channels
- one long-lived Tele Cli service per state directory
- one Codex App Server child supervised by that service
- multiple persisted sessions mapped to Telegram topics or local channels

### Out of scope for this product line

- multi-user access control
- hosted SaaS deployment
- Windows support
- silent destructive repair of ambiguous state

## Product Principles

### Single-operator first

The machine belongs to the operator. Tele Cli should optimize for speed, directness, and low ceremony for that one person.

### Telegram-first, not Telegram-only

Telegram is the main remote interface, but install, recovery, update, and detailed status live in the local app shell.

### One interactive shell

Interactive local entry points should converge on the same app shell instead of scattering behavior across raw shell prompts.

### Explicit recovery

When locks, sessions, approvals, or subprocess state look unsafe, Tele Cli should explain the problem and surface a recovery path instead of guessing.

### Portable state and docs

Specs, code, and docs must avoid developer-specific filesystem paths. All examples should use relative paths, config-driven paths, or symbolic names.

## Operator Experience

### Remote use

From Telegram, the operator should be able to:

- send normal requests
- create a fresh session with `/new`
- inspect status with `/status`
- inspect recent sessions with `/sessions`
- stop or abort work with `/stop` and `/abort`
- answer approval prompts if they ever appear

### Local use

From the machine, the operator should be able to:

- launch `tele-cli` into the app shell
- run setup, update, and uninstall through that same shell when interactive
- open a local chat session for direct use without Telegram
- send proactive Telegram text, images, or files by session/channel name

## System Model

Tele Cli is composed of:

1. an installer and service registration layer
2. a host-managed Tele Cli service
3. a local interactive app shell
4. a Telegram integration
5. a Codex App Server integration
6. durable local state and logs

The host-managed service is the source of continuity.
The app shell is the source of operator control.
Telegram and local chat are user-facing transports.
Codex App Server is the execution engine.

## State Model

The default state directory contains:

- `auth.json`
- `config.json`
- `runtime.json`
- `sessions.json`
- `approvals.json`
- `codex_server.json`
- `telegram_updates.json`
- `app.lock`
- `setup.lock`
- `recovery.log`
- `terminal.log`
- `performance.log`
- `app_server_notifications.log`
- `memory/sessions/<session_id>.short_memory.md`

These files exist to preserve continuity, expose health, and support explicit recovery after crashes or interrupted setup.

## Default Runtime Policy

Unless the operator overrides `config.json`, Tele Cli should start Codex threads with:

- `sandbox_mode = "danger-full-access"`
- `approval_policy = "never"`

This reflects the product assumption that Tele Cli runs on the operator's own device and acts on the operator's behalf.

## Success Criteria

Tele Cli is succeeding when:

- Telegram remains responsive while the host is healthy
- the app shell can always explain setup and runtime state
- session-to-thread mappings survive restart when safe
- stale or conflicting state is surfaced clearly
- the operator can review logs and understand failures

## Spec Set

The detailed behavior is split into these documents:

- `spec/ux_spec.md`: user-facing product and interaction contract
- `spec/full_ui_migration_spec.md`: interactive app shell as the primary control surface
- `spec/full_ui_migration_test_plan.md`: tests for the app shell migration
- `spec/codex_app_server_integration_spec.md`: Codex App Server architecture and routing
- `spec/codex_app_server_integration_test_plan.md`: tests for the runtime integration
- `spec/services_and_lifecycle_spec.md`: service ownership, recovery, and lifecycle rules
