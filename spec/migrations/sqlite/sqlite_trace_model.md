# SQLite Trace Model

## Goal

Make every user request reconstructable without parsing flat files.

The trace model must explain:

- what request came in
- which local session handled it
- which Codex thread and turn were used
- what app-server notifications arrived
- what Telegram sends, edits, deletes, and retries occurred
- where latency accumulated

## Trace Identity

Each inbound user request creates one `trace_id`.

That `trace_id` remains stable for the entire lifecycle of the request, including:

- inbound Telegram update handling
- local session resolution
- thread resume or thread start
- turn start or turn steer
- app-server notifications
- outbound Telegram operations
- completion, failure, or timeout

## Timing Model

Every event should capture timing from at least one of these stages:

- `received_at`
- `handled_at`

For outbound Telegram operations, also capture:

- `created_at`
- `claimed_at`
- `completed_at`

These pairs answer different questions:

- app-server late vs reducer late
- queueing delay vs API latency
- transport ingress vs service handling

## Event Sources

Allowed `source` values:

- `telegram_inbound`
- `telegram_outbound`
- `service`
- `app_server`
- `storage`
- `sleep`

## Event Types

Examples of useful event types:

- `telegram.update.received`
- `session.resolved`
- `trace.started`
- `thread.resume.requested`
- `thread.resume.completed`
- `turn.start.requested`
- `turn.start.completed`
- `turn.steer.requested`
- `turn.steer.completed`
- `app_server.notification`
- `telegram.queue.enqueued`
- `telegram.queue.claimed`
- `telegram.api.send.completed`
- `telegram.api.edit.completed`
- `telegram.api.delete.completed`
- `telegram.api.failed`
- `trace.completed`
- `trace.failed`

The event taxonomy should stay stable enough for investigation queries and dashboards.

## Correlation Keys

Useful correlation fields:

- `trace_id`
- `session_id`
- `thread_id`
- `turn_id`
- `item_id`
- `chat_id`
- `topic_id`
- `message_group_id`
- `telegram_message_id`
- `run_id`

Not every event has every key, but every event should carry all keys already known at the time of logging.

## Payload Policy

Small payloads may be stored inline in `payload_json`.

Large payloads should be split like this:

- `payload_preview` inline
- raw body in artifact
- `artifact_id` on the event row

This keeps traces searchable without bloating the DB.

Apply this rule to:

- large inbound user text
- captions
- inbound media descriptors
- app-server notifications
- outbound Telegram payloads
- command output

Default limits:

- previews: 1024 UTF-8 bytes
- general inline payloads: 8192 UTF-8 bytes
- queue payloads: 4096 UTF-8 bytes

## Artifact Reference Contract

When a payload spills, the system should use one universal artifact reference contract instead of inventing per-table ad hoc shapes.

Required fields:

- `storage`
- `artifact_id`
- `kind`

Optional fields:

- `relpath`
- `size_bytes`
- `preview`

Reader rule:

- any subsystem receiving a payload must first determine whether it is inline content or an artifact reference
- if it is an artifact reference, it must resolve the payload through the artifact store before further processing

## Trace Completion

A trace completes when one of these outcomes is persisted:

- `completed`
- `failed`
- `interrupted`
- `cancelled`
- `timed_out`

`completed_at` must be set when the final outcome is known.

## Investigation Queries

The trace model must support fast answers to questions like:

- when did the inbound Telegram update arrive?
- when did the request become a Codex turn?
- when did the first commentary delta arrive at transport?
- when did the service first render visible progress?
- how long did Telegram retries delay delivery?
- did a completed turn emit late notifications afterward?

## Raw Export

The DB should support exporting one trace and all referenced artifacts as a bounded trace bundle for debugging. The export itself belongs in the artifact tree and should be referenced by an `artifacts` row.
