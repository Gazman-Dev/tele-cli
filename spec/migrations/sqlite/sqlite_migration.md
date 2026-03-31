# SQLite Migration Spec

## Goal

Move Tele Cli runtime state and diagnostics into SQLite-backed operational storage without losing local session continuity, short-memory continuity, or queue durability.

This migration is a hard cutover. Backward compatibility with the current JSON-state model is not a goal.

## Scope

This migration covers:

- sessions
- approvals
- Telegram update dedupe
- outbound Telegram queue
- service run records
- event and trace logging
- logical Telegram message persistence

This migration does not require:

- moving user-editable workspace files such as `personality.md`, `rules.md`, `workspace/long_memory.md`, or `AGENT.md`
- storing large raw payloads directly in SQLite

## Required Outcomes

After migration:

- all runtime ids are durably persisted in SQLite
- queue state survives restart and update
- session to topic or 1:1 mapping survives restart and update
- trace reconstruction no longer depends on parsing JSONL text files
- local short-memory file mapping remains keyed by local `session_id`
- future schema changes are managed by versioned migration scripts in the repository

## Migration Principles

### No split-brain state

The system should not treat JSON and SQLite as independent sources of truth long term.

### Hard cutover

Once a deployment has initialized SQLite successfully, SQLite becomes the only authoritative runtime store.

Legacy JSON state may be imported once and then ignored.

### Preserve session identity

Local `session_id` remains the stable identity for:

- Telegram topic or 1:1 mapping
- short-memory file mapping
- trace grouping

### Preserve thread continuity when possible

`thread_id` and `active_turn_id` must be persisted transactionally with session updates.

### No developer-local paths

All artifact references are relative to the state directory.

## Cutover Rules

On service startup after migration:

1. initialize SQLite
2. run schema migrations
3. if the DB is empty, import the current legacy JSON state into SQLite
4. mark bootstrap completion in SQLite
5. continue using SQLite as the authoritative runtime store

If legacy files remain present after successful import, they are legacy backups only and must not be treated as the active source of truth.

## Legacy Imports

Import these legacy files if present:

- `sessions.json`
- `approvals.json`
- `telegram_updates.json`
- `runtime.json`
- `codex_server.json`

Import strategy:

- preserve ids and timestamps exactly when available
- record import provenance in trace or event metadata where useful

Import does not need to handle every historical edge case perfectly. It only needs to faithfully bootstrap the currently deployed system into the new store.

## Operational Queue Migration

The current implicit in-memory and inline Telegram delivery behavior should move to a durable outbound queue.

The SQLite queue must support:

- global ordering across sessions
- retries with backoff
- claim and release semantics
- restart recovery for in-flight work
- coalescing of superseded unsent updates when safe

## Future Migrations

Future DB changes must use repository-managed migration scripts.

Required components:

- migration directory committed in the repo
- ordered migration ids
- migration runner in application code
- `schema_migrations` table as the applied-history source of truth

Rules:

- migrations are append-only
- shipped migration files are immutable
- a migration either commits fully or fails fully
- destructive changes must be expressed as explicit migrations, not silent startup rewrites

Recommended script naming:

- `0001_initial.sql`
- `0002_add_trace_parent.sql`
- `0003_add_queue_dedupe.sql`

Recommended process:

1. add new migration script
2. update schema spec if the target model changed
3. ship code that can work with the migrated schema
4. let startup run unapplied migrations automatically

## Completion Criteria

The migration is complete when:

- runtime state no longer depends on `sessions.json`
- outbound Telegram delivery state survives restart
- trace queries can reconstruct a full request lifecycle from SQLite
- artifact references are queryable from SQLite
- the service can recover from restart during an active turn without losing the local session mapping
- future releases can evolve the DB through ordered migration scripts rather than one-off conversion code
