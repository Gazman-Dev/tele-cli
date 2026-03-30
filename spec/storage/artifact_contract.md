# Artifact Contract

## Goal

Define one universal contract for how Tele Cli represents, stores, and reads spilled payloads.

This contract exists so every subsystem handles large payloads the same way instead of inventing local formats.

## Scope

The contract applies to any payload that may exceed inline storage limits, including:

- inbound user text
- inbound captions
- inbound media descriptors
- app-server requests
- app-server notifications
- command output
- outbound Telegram payloads
- exported trace bundles

## Inline Or Artifact

Every payload must be representable in one of two forms:

1. inline content
2. artifact reference

The caller must know which form it is dealing with before it attempts to read or process the payload.

## Default Size Policy

Unless a narrower limit is defined for a specific field:

- preview limit: `1024` UTF-8 bytes
- general inline payload limit: `8192` UTF-8 bytes
- queue payload limit: `4096` UTF-8 bytes
- rendered chunk cache limit: `4096` UTF-8 bytes

Anything above the applicable limit must spill to an artifact.

## Artifact Reference Envelope

When a payload spills, the canonical reference format is:

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

## Required Fields

- `storage`
  - must be the literal value `artifact`
- `artifact_id`
  - durable primary identifier for the spilled payload
- `kind`
  - artifact kind from the allowed artifact-kind set

## Optional Fields

- `relpath`
  - relative path under the state directory
- `size_bytes`
  - raw payload size
- `preview`
  - bounded readable preview

## Artifact Kinds

The allowed kind set is:

- `app_server_payload`
- `telegram_payload`
- `command_output`
- `media_download`
- `trace_export`
- `migration_snapshot`

## Reader Contract

Every subsystem that accepts a payload must follow one reader path:

1. determine whether the payload is inline content or an artifact reference
2. if inline, use it directly
3. if artifact reference, resolve by `artifact_id`
4. read the artifact file relative to the state directory
5. decode according to `kind` and stored metadata

Code must not bypass this contract by directly opening guessed file paths.

## Writer Contract

When a payload exceeds its inline limit:

1. compute the bounded preview
2. write the raw body to an artifact file
3. compute hash and size
4. create or update the `artifacts` row
5. replace the inline payload with the artifact reference envelope
6. keep preview text inline where the owning row supports it

## Preview Rules

Previews exist for search, diagnostics, and operator visibility.

Rules:

- previews must be UTF-8 text
- previews must be bounded to the applicable preview limit
- previews must not be treated as the source of truth
- previews should prefer readable truncation over raw binary fragments

## Error Handling

If artifact write fails for an optional payload:

- keep the inline preview
- log the failure
- continue if correctness is not affected

If artifact write fails for a required payload:

- fail the operation
- do not commit partial durable state that depends on the artifact

If artifact read fails:

- surface the failure as an explicit read error
- do not silently substitute the preview as though it were the full payload

## Security And Portability

- store only relative artifact paths
- never persist developer-local absolute paths
- do not trust `relpath` alone; `artifact_id` remains the canonical lookup key

## Implementation Guidance

The application code should provide a single shared artifact-store API that exposes operations like:

- `spill_text(...)`
- `spill_bytes(...)`
- `read_artifact_text(...)`
- `read_artifact_bytes(...)`
- `build_artifact_ref(...)`
- `is_artifact_ref(...)`

That API should be the only supported path for large payload storage and retrieval.
