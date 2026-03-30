# Artifact Storage

## Goal

Keep operational state and query-heavy metadata inside SQLite while keeping large raw payloads, binary media, and debug-heavy bodies on disk.

## Rule

SQLite is the system of record for metadata.

Artifact files are the system of record for large raw bodies.

The database stores:

- artifact kind
- relative path
- size
- hash
- compression flag
- retention metadata
- the row that references the artifact

## Artifact Kinds

Supported artifact kinds:

- `app_server_payload`
- `telegram_payload`
- `command_output`
- `media_download`
- `trace_export`
- `migration_snapshot`

## Directory Layout

Artifacts live under the state directory.

Recommended layout:

- `artifacts/app_server/YYYY/MM/DD/`
- `artifacts/telegram/YYYY/MM/DD/`
- `artifacts/command_output/YYYY/MM/DD/`
- `artifacts/media/YYYY/MM/DD/`
- `artifacts/traces/YYYY/MM/DD/`
- `artifacts/migration/YYYY/MM/DD/`

File names should be generated, not user-derived. The artifact row in SQLite provides the stable lookup path.

## Spill Rules

Small payloads may stay inline in SQLite.

Required default thresholds:

- inline text payload limit: 8 KB UTF-8
- inline preview limit: 1024 UTF-8 bytes
- inline queue payload limit: 4 KB UTF-8
- inline rendered chunk cache limit: 4 KB UTF-8
- spill anything above the applicable inline limit

These limits apply unless a narrower limit is explicitly defined for a specific column or payload type.

Typical spill candidates:

- long command stdout or stderr
- large app-server notifications or request payloads
- large rendered Telegram HTML bodies
- large inbound Telegram text or captions
- raw inbound Telegram media metadata
- raw outbound queue payloads when they exceed inline size limits
- exported trace bundles

## Universal Artifact Reference

Any payload that spills out of SQLite must be referencable in a universal, structured way.

Recommended JSON shape:

```json
{
  "storage": "artifact",
  "artifact_id": "art_123",
  "kind": "telegram_payload",
  "relpath": "artifacts/telegram/2026/03/30/art_123.json",
  "size_bytes": 24576,
  "preview": "First 1024 bytes of readable text..."
}
```

Rules:

- this structure may appear inside queue payloads, events, or any future payload envelope
- `artifact_id` is the canonical durable key
- `relpath` is included for operability and debugging, not as the primary key
- `preview` is optional when the referencing row already stores a dedicated preview column

The service should have one universal reader path:

1. if the payload is inline text, use it directly
2. if the payload is an artifact reference, resolve `artifact_id`
3. read the artifact file relative to the state directory
4. decode according to artifact kind and metadata

## Write Contract

When a payload must spill:

1. write the artifact file under the target artifact directory
2. compute size and hash
3. insert or update the `artifacts` row
4. store the `artifact_id` on the referencing row
5. keep a short inline preview in SQLite when useful

If the artifact is optional and the write fails:

- keep the inline preview
- record an event describing the spill failure

If the artifact is required for correctness:

- fail the enclosing transaction
- leave the operational row pending or unmodified

## Large Inbound User Content

Large user messages, captions, and inbound media descriptors must not be stored as unbounded inline text in SQLite.

Rules:

- keep a short preview inline for search and diagnostics
- spill the full raw inbound body to an artifact
- link the artifact from the trace event, update row, or message row that references it

Required thresholds:

- inbound user text above 8 KB must spill
- inbound captions above 4 KB must spill
- media descriptors above 8 KB must spill

The same rule applies whether the payload came from:

- a long Telegram text message
- a long caption attached to media
- a structured media descriptor
- a future non-Telegram transport

## Path Rules

- Store artifact paths relative to the state directory.
- Do not persist developer-local absolute paths.
- User-facing docs and debug outputs should refer to logical artifact kind and relative path.

## Content Rules

Do not write unbounded append-only blobs into a single artifact file.

Instead:

- create one artifact per payload snapshot or exported bundle
- let SQLite relate them through foreign keys or stored ids

This keeps pruning simple and prevents one hot file from growing forever.
