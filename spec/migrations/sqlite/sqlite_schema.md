# SQLite Schema Spec

## Database File

- `tele_cli.db`

## Schema Ownership

SQLite schema is owned by versioned migration scripts in the repository.

Recommended layout:

- `src/storage/migrations/0001_initial.sql`
- `src/storage/migrations/0002_*.sql`
- `src/storage/migrations/0003_*.sql`

The numeric prefix is the durable migration id. It must be monotonic and never reused.

## Pragmas

Recommended:

- `journal_mode = WAL`
- `synchronous = NORMAL`
- `foreign_keys = ON`
- `busy_timeout = 5000`

## General Rules

- Use UTC ISO 8601 timestamps in text columns.
- Use generated ids for durable rows instead of relying on row order.
- Keep large raw payloads out of SQLite whenever practical. Use `artifacts` plus inline previews.
- Prefer explicit unique constraints over application-only assumptions.
- Any text column that can grow with user input or rendered output must have a spill-to-artifact policy.
- Default inline limits:
  - preview columns: 1024 UTF-8 bytes
  - general payload columns: 8192 UTF-8 bytes
  - queue payload columns: 4096 UTF-8 bytes
  - rendered chunk cache columns: 4096 UTF-8 bytes

## Table: `schema_migrations`

Tracks applied schema changes.

Columns:

- `id INTEGER PRIMARY KEY`
- `version INTEGER NOT NULL UNIQUE`
- `name TEXT NOT NULL UNIQUE`
- `checksum TEXT NOT NULL`
- `applied_at TEXT NOT NULL`

Rules:

- one row per applied migration script
- `version` is the ordered schema migration number
- `checksum` protects against script drift after a migration has shipped

## Table: `service_runs`

One row per service process lifetime.

Columns:

- `run_id TEXT PRIMARY KEY`
- `started_at TEXT NOT NULL`
- `stopped_at TEXT`
- `version TEXT`
- `pid INTEGER`
- `hostname TEXT`
- `state_dir TEXT`
- `exit_reason TEXT`

Indexes:

- `idx_service_runs_started_at(started_at)`

## Table: `sessions`

Durable local session records.

Columns:

- `session_id TEXT PRIMARY KEY`
- `transport TEXT NOT NULL`
- `transport_user_id INTEGER`
- `transport_chat_id INTEGER`
- `transport_topic_id INTEGER`
- `transport_channel TEXT`
- `attached INTEGER NOT NULL`
- `status TEXT NOT NULL`
- `thread_id TEXT`
- `active_turn_id TEXT`
- `last_completed_turn_id TEXT`
- `current_trace_id TEXT`
- `instructions_dirty INTEGER NOT NULL`
- `last_seen_generation INTEGER NOT NULL`
- `created_at TEXT NOT NULL`
- `last_user_message_at TEXT`
- `last_agent_message_at TEXT`

Constraints:

- one attached writable Telegram session per `(transport_chat_id, transport_topic_id)`
- one attached writable local session per `transport_channel`
- `active_turn_id` may be set only when `thread_id` is also set

Indexes:

- `idx_sessions_transport_chat_topic(transport, transport_chat_id, transport_topic_id)`
- `idx_sessions_transport_channel(transport, transport_channel)`
- `idx_sessions_thread_id(thread_id)`
- `idx_sessions_active_turn_id(active_turn_id)`
- `idx_sessions_last_completed_turn_id(last_completed_turn_id)`

## Table: `session_short_memory`

Maps sessions to short-memory files.

Columns:

- `session_id TEXT PRIMARY KEY REFERENCES sessions(session_id) ON DELETE CASCADE`
- `short_memory_relpath TEXT NOT NULL`
- `updated_at TEXT NOT NULL`

## Table: `telegram_updates`

Dedupes inbound Telegram updates.

Columns:

- `update_id INTEGER PRIMARY KEY`
- `chat_id INTEGER`
- `topic_id INTEGER`
- `received_at TEXT NOT NULL`
- `processed_at TEXT`
- `status TEXT NOT NULL`
- `payload_preview TEXT`
- `artifact_id TEXT`

Rules:

- full raw inbound update bodies may spill to artifacts
- `payload_preview` is for bounded inline search and diagnostics only
- if the inbound raw update body exceeds 8192 UTF-8 bytes, it must spill to an artifact

Indexes:

- `idx_telegram_updates_chat_topic(chat_id, topic_id)`
- `idx_telegram_updates_processed_at(processed_at)`

## Table: `approvals`

Durable Codex approval state.

Columns:

- `request_id INTEGER PRIMARY KEY`
- `session_id TEXT REFERENCES sessions(session_id)`
- `thread_id TEXT`
- `turn_id TEXT`
- `trace_id TEXT REFERENCES traces(trace_id)`
- `method TEXT NOT NULL`
- `params_json TEXT NOT NULL`
- `status TEXT NOT NULL`
- `created_at TEXT NOT NULL`
- `updated_at TEXT NOT NULL`
- `resolved_at TEXT`

Rules:

- `params_json` must stay within the general payload limit
- if approval parameters exceed 8192 UTF-8 bytes, the row must use the same artifact-reference contract as other large payloads

Indexes:

- `idx_approvals_status(status)`
- `idx_approvals_session_id(session_id)`

## Table: `traces`

One row per user request lifecycle.

Columns:

- `trace_id TEXT PRIMARY KEY`
- `session_id TEXT REFERENCES sessions(session_id)`
- `thread_id TEXT`
- `turn_id TEXT`
- `parent_trace_id TEXT REFERENCES traces(trace_id)`
- `chat_id INTEGER`
- `topic_id INTEGER`
- `user_text_preview TEXT`
- `started_at TEXT NOT NULL`
- `completed_at TEXT`
- `outcome TEXT`

Rules:

- `user_text_preview` is a bounded preview only
- the full inbound user message belongs in events and artifacts, not in the trace row
- `user_text_preview` must not exceed 1024 UTF-8 bytes

Indexes:

- `idx_traces_session_id(session_id)`
- `idx_traces_thread_turn(thread_id, turn_id)`
- `idx_traces_started_at(started_at)`

## Table: `events`

Normalized event stream metadata.

Columns:

- `event_id INTEGER PRIMARY KEY`
- `trace_id TEXT REFERENCES traces(trace_id)`
- `run_id TEXT REFERENCES service_runs(run_id)`
- `source TEXT NOT NULL`
- `event_type TEXT NOT NULL`
- `received_at TEXT NOT NULL`
- `handled_at TEXT`
- `session_id TEXT REFERENCES sessions(session_id)`
- `thread_id TEXT`
- `turn_id TEXT`
- `item_id TEXT`
- `source_event_id TEXT`
- `chat_id INTEGER`
- `topic_id INTEGER`
- `message_group_id TEXT`
- `telegram_message_id INTEGER`
- `payload_json TEXT`
- `payload_preview TEXT`
- `artifact_id TEXT`

Rules:

- `payload_json` is only for bounded inline payloads
- full raw notification or request bodies must spill to artifacts once they exceed inline limits
- `payload_preview` must remain small enough for indexed investigation queries
- `payload_preview` must not exceed 1024 UTF-8 bytes
- `payload_json` must not exceed 8192 UTF-8 bytes

Indexes:

- `idx_events_trace_id(trace_id)`
- `idx_events_thread_turn(thread_id, turn_id)`
- `idx_events_session_id(session_id)`
- `idx_events_received_at(received_at)`
- `idx_events_source_type(source, event_type)`

## Table: `telegram_outbound_queue`

Global serialized outbound Telegram queue.

Columns:

- `queue_id TEXT PRIMARY KEY`
- `created_at TEXT NOT NULL`
- `available_at TEXT NOT NULL`
- `status TEXT NOT NULL`
- `op_type TEXT NOT NULL`
- `chat_id INTEGER NOT NULL`
- `topic_id INTEGER`
- `session_id TEXT REFERENCES sessions(session_id)`
- `trace_id TEXT REFERENCES traces(trace_id)`
- `message_group_id TEXT`
- `telegram_message_id INTEGER`
- `dedupe_key TEXT`
- `priority INTEGER NOT NULL`
- `disable_notification INTEGER NOT NULL`
- `payload_json TEXT NOT NULL`
- `attempt_count INTEGER NOT NULL`
- `last_error TEXT`
- `claimed_by_run_id TEXT REFERENCES service_runs(run_id)`
- `claimed_at TEXT`
- `completed_at TEXT`

Rules:

- `payload_json` is for bounded queue payloads only
- oversized send or edit bodies must spill to artifacts and be referenced indirectly by the queue payload
- the queue must never rely on an unbounded inline HTML body
- `payload_json` must not exceed 4096 UTF-8 bytes
- the queue payload should carry an inline body only when it fits within the queue limit; otherwise it should carry an artifact reference envelope

Indexes:

- `idx_telegram_queue_status_available(status, available_at)`
- `idx_telegram_queue_chat(status, chat_id, topic_id)`
- `idx_telegram_queue_trace_id(trace_id)`
- `idx_telegram_queue_dedupe_key(dedupe_key)`

## Table: `telegram_message_groups`

Logical Telegram message group state.

Columns:

- `message_group_id TEXT PRIMARY KEY`
- `session_id TEXT REFERENCES sessions(session_id)`
- `trace_id TEXT REFERENCES traces(trace_id)`
- `chat_id INTEGER NOT NULL`
- `topic_id INTEGER`
- `logical_role TEXT NOT NULL`
- `status TEXT NOT NULL`
- `created_at TEXT NOT NULL`
- `updated_at TEXT NOT NULL`
- `finalized_at TEXT`

Indexes:

- `idx_telegram_message_groups_session(session_id)`
- `idx_telegram_message_groups_trace(trace_id)`

## Table: `telegram_message_chunks`

Physical Telegram messages belonging to a logical message.

Columns:

- `message_group_id TEXT NOT NULL REFERENCES telegram_message_groups(message_group_id) ON DELETE CASCADE`
- `chunk_index INTEGER NOT NULL`
- `telegram_message_id INTEGER`
- `rendered_html TEXT`
- `artifact_id TEXT`
- `created_at TEXT NOT NULL`
- `updated_at TEXT NOT NULL`
- `deleted_at TEXT`

Rules:

- `rendered_html` may contain bounded inline content for fast sync operations
- if the rendered chunk grows too large for comfortable inline storage, keep only a preview or current body needed for delivery and spill the full render snapshot to an artifact
- `rendered_html` must not exceed 4096 UTF-8 bytes when stored inline

Primary key:

- `(message_group_id, chunk_index)`

Indexes:

- `idx_telegram_chunks_message_id(telegram_message_id)`

## Table: `artifacts`

External raw payload metadata.

Columns:

- `artifact_id TEXT PRIMARY KEY`
- `kind TEXT NOT NULL`
- `relpath TEXT NOT NULL UNIQUE`
- `size_bytes INTEGER NOT NULL`
- `sha256 TEXT`
- `created_at TEXT NOT NULL`
- `expires_at TEXT`
- `compressed INTEGER NOT NULL`

Indexes:

- `idx_artifacts_kind_created(kind, created_at)`
- `idx_artifacts_expires_at(expires_at)`

Intended uses include:

- large inbound user text
- media descriptors and downloads
- large app-server request or notification bodies
- large Telegram outbound HTML payloads
- command output snapshots

## Required Foreign-Key Behavior

- Deleting a session must cascade into `session_short_memory`.
- Deleting a message group must cascade into `telegram_message_chunks`.
- Events and traces should normally be pruned in order rather than through deep cascading deletes.
