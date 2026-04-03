# Logging Spec

## 1. Goal

Define a logging system that is:

- queryable
- durable across restarts and updates
- safe under concurrent runtime activity
- bounded in disk usage
- useful both for live operator debugging and postmortem diagnosis

The logging system must have one clear source of truth.

## 2. Core Decision

SQLite is the canonical log store.

Implications:

- all operational diagnostics must be written to SQLite first
- text logs are optional mirrors, not the source of truth
- debugging should normally start from SQLite queries, not ad hoc file tails
- retention and pruning policy must be defined primarily around SQLite rows and linked artifacts

## 3. Logging Layers

Tele Cli logging should be split into three layers:

1. canonical structured logs in SQLite
2. optional human-readable mirrors on disk
3. artifact files for oversized payloads

### 3.1 Canonical structured logs

Stored in:

- `service_runs`
- `traces`
- `events`
- `telegram_outbound_queue`
- `telegram_message_groups`
- `telegram_message_chunks`

These tables are the primary operational history.

### 3.2 Optional text mirrors

Allowed text mirrors:

- `terminal.log`
- `performance.log`

Rules:

- mirrors must be derivable from SQLite-backed events or performance records
- loss or truncation of a mirror file must not lose canonical diagnostics
- mirror files must never be the only place where a runtime event exists

### 3.3 Artifact files

Artifacts hold:

- oversized event payloads
- raw app-server payload snapshots
- large command output
- exported trace bundles

Artifacts remain referenced from SQLite through durable metadata.

## 4. Logging Principles

### 4.1 Source-of-truth rule

If an event matters for debugging, recovery, operator support, queue behavior, or lifecycle reconstruction, it must be present in SQLite.

Examples:

- inbound Telegram update acceptance and dedupe
- session resolution and session replacement
- request start, failure, and completion
- Codex app-server notification intake
- Telegram queue enqueue, claim, retry, complete, and fail
- outbound Telegram API send, edit, delete, and typing actions
- recovery actions and degraded-state transitions
- login and auth-required transitions
- service startup, restart, shutdown, and crash recovery

### 4.2 Mirror-only rule

Text files may improve ergonomics, but they are secondary.

Allowed reasons to keep a text mirror:

- quick `tail -f` workflows
- early-crash visibility before a richer UI exists
- local operator convenience

Forbidden design:

- logging something only to `terminal.log`
- relying on `performance.log` for data that is not also queryable from SQLite

### 4.3 Bounded-payload rule

Inline event payloads stay bounded.

When payloads exceed inline limits:

- keep a preview inline
- spill full content to an artifact
- store the artifact reference in SQLite

### 4.4 Portable-path rule

SQLite must store relative artifact paths or artifact ids only.

Developer-specific absolute paths must not appear in durable log design.

## 5. Event Model

The `events` table is the normalized event stream.

Each event should answer:

- what happened
- where it happened
- when it happened
- what request or run it belongs to
- what identifiers are needed to correlate it

Required fields already align with this:

- `event_id`
- `trace_id`
- `run_id`
- `source`
- `event_type`
- `received_at`
- `handled_at`
- `session_id`
- `thread_id`
- `turn_id`
- `source_event_id`
- `chat_id`
- `topic_id`
- `message_group_id`
- `telegram_message_id`
- `payload_json`
- `payload_preview`
- `artifact_id`

## 6. Trace Model

The `traces` table is the top-level request timeline.

Rules:

- every inbound user request gets a `trace_id`
- all significant events from that request should reference the same `trace_id`
- a trace should be sufficient to reconstruct the full lifecycle of one operator-visible request

Expected linked phases:

- inbound update received
- session resolved
- request sent to Codex
- app-server activity and turn state
- Telegram delivery actions
- final outcome

## 7. Service-Run Model

The `service_runs` table is the top-level process lifetime log.

Rules:

- every service process start creates one `run_id`
- events emitted by that process should carry `run_id` where practical
- service stop or crash outcome should be reflected on the run row and in events

This allows separation of:

- one request lifecycle
- one service lifetime
- one long-lived Telegram/Codex session

## 8. Performance Logging

Performance data should become structured diagnostics first, mirror second.

Preferred model:

- latency and throughput records are emitted as structured events
- `performance.log` becomes an optional projection of those events

Minimum required performance event families:

- Telegram outbound operation timing
- queue wait timing
- app-server request/turn timing
- full request latency
- session recovery timing

If a plain text performance line exists, the equivalent structured record must also exist.

## 9. Terminal Log Role

`terminal.log` may remain, but only as a convenience mirror.

Recommended contents:

- user-visible assistant output
- inbound operator messages
- short operator-readable status lines

Rules:

- it must not be required for core debugging
- it may be rotated aggressively
- it may omit high-volume internal detail already preserved in SQLite

`terminal.log` is for readability, not canonical diagnostics.

## 10. Required Event Coverage

The runtime must emit structured events for these areas.

### 10.1 Telegram inbound

- poll started
- poll result
- poll error
- update accepted
- update deduped
- update bound to trace
- attachment saved

### 10.2 Session routing

- session resolved
- session reused
- session created
- session detached
- session replaced
- session recovered

### 10.3 Codex runtime

- request started
- request failed
- reply started
- reply delta observed when relevant for diagnostics
- reply finished
- thread status changed when operationally relevant
- auth required
- login callback received
- login completed

### 10.4 Telegram outbound

- queue enqueue
- queue claim
- queue retry scheduled
- queue completed
- queue failed
- API send started and completed
- API edit started and completed
- API delete started and completed
- typing started and completed

### 10.5 Lifecycle and recovery

- service starting
- service started
- service stopping
- service stopped
- service degraded
- service recovered
- Telegram degraded/backoff transitions
- Codex degraded/backoff/auth transitions
- lock conflict and recovery actions

## 11. Retention And Pruning

Logging must be bounded.

### 11.1 General rules

- prune by age and volume
- prune oldest first
- preserve recent traces in full fidelity
- never prune rows needed by still-active sessions, in-flight traces, or queued Telegram work

### 11.2 Recommended retention defaults

Recommended starting policy:

- `service_runs`: keep 90 days
- `traces`: keep 30 days
- `events`: keep 30 days
- queue rows tied to completed outbound work: keep 14 days
- text mirror logs: keep 7 rolling daily files or a small fixed byte cap
- artifacts referenced by retained rows: keep as long as their referencing rows remain

### 11.3 Pruning safety

Pruning must:

- skip active traces
- skip active queue entries
- skip rows referenced by retained artifacts
- run in bounded batches
- log pruning summaries back into SQLite

## 12. Failure Behavior

### 12.1 SQLite unavailable

If SQLite is temporarily unavailable:

- the runtime should degrade explicitly
- operator-visible status should reflect logging degradation if it affects recoverability
- a temporary emergency mirror may be written locally, but must be treated as best-effort only

SQLite failure is a degraded state, not a reason to silently fall back to file-only logging as a normal mode.

### 12.2 Mirror write failure

If `terminal.log` or `performance.log` fails:

- the runtime must continue if SQLite logging succeeds
- mirror failure may be logged as a structured event

## 13. Query UX

Tele Cli should expose logs through product UX, not just raw SQLite inspection.

Desired operator capabilities:

- recent service status
- recent failed traces
- recent recovery events
- recent Telegram queue failures
- events for one `trace_id`
- events for one chat/topic
- events for one session

This should ultimately be exposed through a dedicated CLI command or app-shell view.

## 14. Migration Direction

Current state appears transitional:

- SQLite already stores `service_runs`, `traces`, and `events`
- `terminal.log` and `performance.log` still matter too much during debugging

Target direction:

1. ensure every meaningful log class is emitted to SQLite
2. reduce file logs to convenience mirrors
3. add retention and pruning
4. add first-class log inspection UX

## 15. Non-Goals

This spec does not require:

- remote log shipping
- external observability vendors
- full-text indexing of all payload bodies
- retaining every raw payload inline forever

## 16. Acceptance Criteria

The logging design is acceptable when:

- a production issue can be debugged from SQLite without depending on text logs
- one request can be reconstructed from a single `trace_id`
- queue failures, rate limits, and recovery actions are queryable without grepping text files
- text mirror loss does not erase important diagnostics
- pruning keeps storage bounded without deleting active operational data
