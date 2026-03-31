# Services And Lifecycle Spec

## Status

- Status: active working spec
- Date: March 31, 2026
- Related docs:
  - `spec.md`
  - `spec/codex_app_server_integration_spec.md`
  - `docs/wiki/Operations.md`

## Goal

Tele Cli should behave like a dependable local service, not like a fragile foreground script.

When the host is healthy, the operator should be able to assume:

- Telegram remains reachable
- the runtime has a single clear owner
- Codex can be restarted or recovered without losing all context

## Core Invariants

### One service owner per state directory

For a given state directory, exactly one live Tele Cli owner may hold runtime ownership at a time.

### One Codex supervisor per service owner

That service owner may supervise at most one Codex App Server child for the same state directory.

### Telegram remains first-class

Telegram listening should not depend on an active turn and should stay up through Codex login-required and restart scenarios whenever possible.

### Session continuity is durable

Session records, thread mappings, and approval state must survive process restarts and app upgrades.

### Recovery is explicit

When the runtime cannot safely preserve continuity, it must mark degradation and expose operator choices instead of guessing.

## Components

The lifecycle contract covers:

1. installer and updater
2. host service registration and restart policy
3. Tele Cli service process
4. Codex App Server child
5. durable state directory

## State Files

Expected runtime files include:

- `app.lock`
- `setup.lock`
- `auth.json`
- `config.json`
- `runtime.json`
- `sessions.json`
- `approvals.json`
- `codex_server.json`
- `telegram_updates.json`
- `recovery.log`
- `terminal.log`
- `performance.log`
- `app_server_notifications.log`

## Lifecycle States

### Service states

- `STARTING`
- `RUNNING`
- `DEGRADED`
- `RESTARTING`
- `STOPPING`
- `STOPPED`
- `FAILED`

### Telegram states

- `STARTING`
- `RUNNING`
- `BACKOFF`
- `DEGRADED`
- `STOPPED`

### Codex states

- `STOPPED`
- `STARTING`
- `INITIALIZING`
- `RUNNING`
- `AUTH_REQUIRED`
- `BACKOFF`
- `DEGRADED`
- `STOPPING`

### Session states

- `ACTIVE`
- `IDLE`
- `RUNNING_TURN`
- `WAITING_APPROVAL`
- `INTERRUPTED`
- `RECOVERING_TURN`
- `DETACHED`
- `DEGRADED`

## Startup Contract

On service start, Tele Cli should:

1. load durable state
2. inspect and acquire runtime ownership
3. mark service state as starting
4. start Telegram polling
5. launch Codex App Server
6. initialize app-server state
7. mark steady-state health or explicit degradation

Important rule:

Telegram startup should not wait for Codex to finish initializing.

## Ownership and Lock Rules

`app.lock` and related metadata should identify:

- PID
- hostname
- username
- process mode
- timestamp
- app version
- child Codex PID when known

When a prior owner is found, Tele Cli should distinguish at least:

- live owner
- stale owner
- ambiguous ownership

If ownership is ambiguous, Tele Cli should not destroy state automatically.

## Setup Re-Entrancy Rules

`setup.lock` should prevent concurrent setup runs and preserve partial progress markers such as:

- npm installed
- Codex installed
- Telegram token saved
- Telegram validated

Interrupted setup should be recoverable through explicit operator choices such as resume or restart.

## Recovery Behavior

### Live conflicting owner

Tele Cli should present a recovery choice rather than silently stealing ownership.

### Stale lock

Tele Cli may reclaim ownership after inspection shows the recorded process is gone.

### Duplicate service registration

The product should detect and surface duplicate host registrations before blindly starting or reinstalling.

### Codex login required

The system should remain operational enough to receive Telegram messages and surface the login-required condition.

### Child crash or initialize failure

The runtime should enter degraded or backoff state, record the error, and attempt bounded restart where appropriate.

## Logging Contract

Recovery and lifecycle decisions should be auditable through local logs, especially:

- `recovery.log`
- `terminal.log`
- `performance.log`
- `app_server_notifications.log`

Logs are part of the product's diagnosability contract, not incidental implementation detail.

## Installer and Update Rules

Installer and updater behavior should:

- bootstrap only what is required to launch the product
- avoid leaving duplicate registrations behind
- preserve durable state unless uninstall is confirmed
- return the operator to the app shell for interactive review and repair

## Done Criteria

This lifecycle contract is correct when:

- only one service owner controls a state directory
- Telegram remains reachable across normal restarts and Codex trouble
- crashes or interrupted setup leave inspectable, recoverable state
- updates and reinstalls do not silently discard ownership or session continuity
