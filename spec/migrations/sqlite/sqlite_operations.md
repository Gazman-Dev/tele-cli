# SQLite Operations Spec

## Goal

Define how SQLite-backed state should be used for live Tele Cli operations, not just passive logging.

## Migration Execution

The service must own migration execution at startup before any runtime components begin mutating durable state.

Rules:

- open the DB
- acquire a DB migration lock
- read `schema_migrations`
- apply any unapplied scripts in ascending version order
- commit each migration atomically
- fail startup if a required migration fails

There is no backward-compatibility requirement for the current JSON-to-SQLite transition. A clean break is acceptable.

For future releases, schema evolution must happen through migration scripts only. No ad hoc startup rewrites.

## Sessions

### Invariant

If `active_turn_id` is set, the session row must also have a valid `thread_id`.

If the session is attached to a Telegram topic or 1:1 chat, that mapping must survive process restart, upgrade, or crash.

### Required transactional updates

The following should happen in one transaction when possible:

- session turn start
- `thread_id` update
- `active_turn_id` update
- trace creation
- live message-group pointer update

## Telegram Outbound Queue

### Model

Telegram outbound delivery is global, not per topic.

All outbound Telegram operations must go through `telegram_outbound_queue`.

### Queue semantics

- one global consumer per service run
- oldest available work first
- retry with exponential backoff
- update `available_at` on retry
- clear `claimed_by_run_id` if the consumer dies
- dedupe where a newer queued operation supersedes an older unsent operation for the same logical message chunk

The queue is global across all Telegram chats and topics. Telegram rate limits are global enough that per-session queues are not sufficient.

### Supported operation types

- `send_message`
- `edit_message`
- `delete_message`
- `typing`
- `send_photo`
- `send_document`

### Claiming rule

Consumer transaction:

1. select eligible row by `status = queued` and `available_at <= now`
2. prefer lower `priority` value first, then oldest `created_at`
2. mark `status = claimed`
3. set `claimed_by_run_id`
4. set `claimed_at`
5. commit

### Completion rule

On success:

- set `status = completed`
- set `completed_at`
- persist returned Telegram ids if any

### Retry rule

On failure:

- increment `attempt_count`
- store `last_error`
- compute next `available_at`
- set `status = queued` if retryable
- set `status = failed` if non-retryable or retry budget exceeded

Retry backoff should respect Telegram `retry_after` when present. Otherwise it should use local backoff policy.

## Message Persistence

Tele Cli should think in logical messages.

A logical message may map to one or more physical Telegram messages due to Telegram limits.

### Live progress

Store live progress in one `telegram_message_group` with:

- `logical_role = live_progress`

Chunk rows represent the physical Telegram messages used for the live content.

The renderer may update the same logical message group repeatedly as content grows, shrinks, or is reformatted.

### Final output

At turn completion:

- finalize the live progress group
- create or update the final group for the user-facing result
- keep both group histories in SQLite even if the Telegram UX collapses them into one displayed result

### Delete semantics

If a message is deleted from Telegram:

- keep the DB row
- set `deleted_at`
- do not erase history

## Trace Logging

### Required timestamps

Every event should support these timing stages when relevant:

- `received_at`
- `handled_at`

For Telegram outbound rows:

- `created_at`
- `claimed_at`
- `completed_at`

This allows exact attribution of latency:

- app-server late
- reducer late
- Telegram late

## App-Server State Handling

The DB-backed session layer must preserve these invariants:

- one attached local session per Telegram topic or 1:1 chat
- a Codex `thread_id` is durable once learned
- a running turn is never persisted without its thread context when that context is known
- restart recovery must prefer preserving continuity over clearing state eagerly

If a session row is inconsistent after crash recovery:

- preserve the local `session_id`
- preserve the short-memory mapping
- mark the session interrupted or degraded
- do not silently erase continuity history

## Artifact Linking

If `payload_json` is too large:

- store a preview inline
- write the raw payload to an artifact file
- set `artifact_id`

## Crash Recovery

On service startup:

- find queue rows claimed by dead runs
- return them to `queued`
- preserve attempt count

For sessions:

- preserve session rows and short-memory mapping
- never drop a session because the service restarted

For active turns:

- if a turn cannot be proven active after restart, mark session degraded or interrupted
- do not silently mark it completed
- log the recovery decision in `events`

For version updates:

- a restart during an active turn must preserve the session row, thread id, turn id, and live message-group state already committed
- post-restart recovery may mark the turn interrupted, but it must not erase the local session identity or short-memory mapping

## Pruning

Pruning should run as background maintenance.

Safe targets:

- completed queue rows older than retention
- old event rows
- expired artifacts

Unsafe targets:

- attached sessions
- active queue rows
- unresolved approvals
- message groups still referenced by live session state
