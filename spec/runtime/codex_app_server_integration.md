# Codex App Server Integration Spec

## Status

- Status: active working spec
- Date: March 31, 2026
- Related docs:
  - `spec.md`
  - `spec/services_and_lifecycle_spec.md`
  - `docs/wiki/Architecture.md`

## Goal

Tele Cli should treat Codex App Server as the long-lived execution runtime and keep transport/session ownership in Tele Cli itself.

The operator should be able to talk to Tele Cli through Telegram or local chat without losing thread continuity every time the UI disconnects.

## Responsibility Split

### Tele Cli owns

- Telegram authorization and pairing
- local chat channels
- session creation and attachment rules
- mapping sessions to Codex thread ids
- persistent local state and logs
- app-server process supervision
- routing user messages, approvals, and status updates to the correct surface

### Codex App Server owns

- thread lifecycle
- turn execution
- streaming agent output
- approval requests when applicable
- account and login state

## Runtime Model

Per state directory, Tele Cli should run:

- one active Tele Cli service owner
- one supervised Codex App Server child
- many persisted Tele Cli sessions
- at most one attached session per Telegram chat/topic or local channel

Sessions are a Tele Cli concept.
Threads are a Codex concept.
Tele Cli must persist the mapping between them.

## Session Model

Each session record should capture at least:

- `session_id`
- transport type
- transport identity such as chat id, topic id, or local channel
- attachment state
- `thread_id`
- active turn state
- streaming output state
- last user and agent timestamps
- short-memory file association
- session status

### Session rules

- the current attached session for a transport receives new unsuffixed user input
- `/new` or equivalent detaches the old session and creates a fresh one
- detached sessions may finish in-flight work
- a session should not be silently rebound to a different thread after an unsafe resume failure

## Thread and Turn Rules

### Thread startup

If a session has no usable `thread_id`, Tele Cli should start a new thread with the configured:

- working directory
- sandbox mode
- approval policy
- personality

### Thread resume

If a session already has a stored `thread_id`, Tele Cli should try to resume it before creating a replacement.

If resume fails:

- Tele Cli should mark the session degraded or otherwise explicit
- Tele Cli should not silently pretend continuity was preserved

### Turn behavior

For a current attached session:

- a new message starts a turn if none is active
- a follow-up during an active turn may steer that turn when supported
- `/stop` or equivalent should interrupt the active turn

## Instruction Model

Tele Cli should prepend or refresh operator instructions in a controlled way, using:

- session-start instructions for brand-new sessions
- refresh instructions when long-lived sessions need updated context
- per-session short-memory files for temporary working notes

The session short-memory file is Tele Cli state, not Codex-native thread metadata.

## Output Delivery Rules

Tele Cli should transform app-server events into operator-facing output with these goals:

- stream meaningful progress without spamming
- preserve final assistant output
- support Telegram-friendly formatting
- keep thinking or commentary streams useful but bounded

Telegram delivery should support:

- partial flushes after idle gaps
- typing indicators
- final formatted replies
- file or image delivery when the operator explicitly asks for it

## Approval Handling

Tele Cli should persist approvals separately from sessions so that:

- pending approvals survive restart
- approval ids can be shown in Telegram
- stale approvals can be marked explicitly

Even with default `approval_policy = "never"`, Tele Cli should keep a compatibility path for approval events from Codex.

## State Files

The integration depends on these durable files:

- `sessions.json`
- `approvals.json`
- `codex_server.json`
- `runtime.json`

`codex_server.json` should represent app-server transport, initialization, account, and last-error state.

## Failure and Recovery Rules

### App-server process failure

If the child process exits unexpectedly:

- Tele Cli should mark Codex degraded or backoff
- attempt bounded restart
- keep Telegram alive when possible

### Resume ambiguity

If Tele Cli cannot prove a prior thread can be resumed safely, it should surface degradation instead of silently replacing history.

### Stale active turns

If a turn appears stuck beyond the configured threshold, Tele Cli may interrupt and recover it, but that recovery should be explicit in session state and logs.

## Default Policy

By default, new threads should use:

- `sandbox_mode = "danger-full-access"`
- `approval_policy = "never"`

This is intentional because Tele Cli runs on the operator's own device.

## Done Criteria

This integration is correct when:

- Telegram and local chat route to durable Tele Cli sessions
- session-to-thread mappings survive restart
- Codex child restarts do not erase session ownership
- failures produce explicit degraded state instead of hidden replacement behavior
