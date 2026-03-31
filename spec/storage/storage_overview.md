# Storage Overview

## Goal

Replace the current mixed JSON-log and JSON-state model with a storage system that is:

- durable across restarts and updates
- queryable for debugging and trace reconstruction
- safe under concurrent session activity
- bounded in disk usage

## Storage Split

Tele Cli storage should be split into two layers:

1. SQLite database
2. Artifact files on disk

SQLite holds:

- durable operational state
- outbound queues
- trace and event metadata
- message identity mapping
- migration state
- workspace metadata and relative paths

Artifact files hold:

- large raw app-server payloads
- large command outputs
- large Telegram payload snapshots
- large inbound user messages and captions
- downloaded user media
- optional exported trace bundles

## Design Rules

### SQLite is the source of truth for:

- session routing
- thread and turn ids
- approval state
- Telegram update dedupe
- Telegram outbound queue
- service run metadata
- event and trace metadata

### Artifact files are the source of truth for:

- oversized raw bodies
- binary media
- raw payloads that should not bloat SQLite

This includes oversized inbound Telegram text, captions, media metadata snapshots, and large outbound rendered bodies.

### Relative paths only

The database stores artifact paths relative to the state directory.

### One state directory

Each Tele Cli state directory owns one SQLite database and one artifact tree.

## Proposed State Layout

Under the state directory:

- `tele_cli.db`
- `artifacts/`
- `memory/`
- `workspace/`
- `artifacts/app_server/`
- `artifacts/telegram/`
- `artifacts/command_output/`
- `artifacts/media/`
- `artifacts/traces/`

The workspace tree is separate from the artifact tree.

Recommended structure:

- `memory/lessons/`
- `memory/sessions/<session_id>.short_memory.md`
- `workspace/`
- `workspace/AGENTS.md`
- `workspace/long_memory.md`
- `workspace/.gitignore`
- `workspace/topics/`
- `workspace/topics/<topic>/`
- `workspace/topics/<topic>/AGENTS.md`
- `workspace/topics/<topic>/.gitignore`

Topic directories are durable workspaces, not temporary cache folders.
The existing Tele Cli memory files remain part of the system alongside the new workspace tree.
Everything under `memory/` is temporary working memory and does not need to be committed.
Durable workspace memory belongs under `workspace/`.

## SQLite Responsibilities

SQLite is not just for logs. It should also drive live operations:

- outbound Telegram queue
- session persistence
- approval persistence
- dedupe of processed Telegram updates
- service recovery after crash or update

## Universal Large-Payload Rule

Large text and binary payloads do not stay inline in operational rows.

Default rule:

- keep only bounded previews inline
- spill full raw content to artifacts
- refer to the artifact through a universal artifact reference structure

This rule applies consistently to:

- inbound user text
- inbound captions
- inbound media descriptors
- app-server payloads
- outbound Telegram payloads
- command output

## Message Model

Tele Cli should treat a reply as one logical message, but map it to one or more physical Telegram messages.

The database must track:

- logical message identity
- chunk ordering
- actual Telegram `message_id`s
- whether a message is live progress, collapsed thinking summary, final answer, or error

## Trace Model

Every incoming user request should create a `trace_id`.

That `trace_id` should link:

- inbound Telegram update
- resolved local session
- Codex thread id
- Codex turn id
- app-server notifications
- outbound Telegram sends and edits
- final completion outcome

## Write Strategy

### Database

- WAL mode enabled
- transactional writes for multi-row state changes
- indexed by session, thread, turn, trace, and chat/topic

### Artifacts

- write file first
- compute hash and size
- insert metadata row in the same logical operation where possible

## Compatibility

Current JSON files should be treated as legacy state during migration.

Target long-term outcome:

- JSON state files retired for operational state
- only config and a minimal emergency recovery marker may remain outside SQLite
