# Retention And Pruning

## Goal

Bound disk usage without deleting state required for live operations, restart recovery, or active debugging.

## Retention Model

Retention uses both:

- age-based expiry
- quota-based pruning

Both rules apply to SQLite rows and artifact files.

## Recommended Defaults

### SQLite Rows

- `events`: 30 days
- `traces`: 30 days
- `service_runs`: 90 days
- completed `telegram_outbound_queue` rows: 14 days
- resolved `approvals`: 30 days
- `telegram_updates`: 14 days after processed

### Artifact Files

- debug payload artifacts: 14 days
- command output artifacts: 14 days
- exported traces: 30 days
- downloaded user media: feature-defined, not generic log retention

## Quotas

Recommended starting quotas:

- SQLite soft target: 256 MB
- Artifact tree soft target: 1 GB
- Single artifact soft target: 10 MB before compression or truncation policy applies

## Safe Pruning Targets

- completed outbound queue rows past retention
- old trace and event rows past retention
- expired artifacts with no required live reference
- orphaned payload artifacts created by failed operations
- stale rendered Telegram payload artifacts

## Unsafe Pruning Targets

- attached sessions
- active or retryable outbound queue rows
- unresolved approvals
- rows needed for active message-group synchronization
- short-memory files for active sessions
- artifacts still referenced by non-pruned rows

## Truncation

Large text payloads should not stay fully inline in SQLite.

For large text rows:

- keep a short preview inline
- persist the full raw text as an artifact
- store the original size and artifact reference

Recommended preview size:

- 512 to 1024 characters

## Maintenance Tasks

Background maintenance should:

- checkpoint WAL
- prune expired queue rows
- prune expired event and trace rows
- prune expired artifacts
- compact orphaned message-group chunks

`VACUUM` should not run in the hot path. Use controlled maintenance windows only.

## Failure Rules

Pruning must be restart-safe.

If pruning is interrupted:

- partial artifact deletes must not leave referenced artifact rows pointing to missing files
- row deletion and artifact deletion should be ordered so references are cleared before physical removal when necessary

The system should prefer leaving extra old data behind rather than risking live-state corruption.
