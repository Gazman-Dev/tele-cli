# Spec Index

This directory is organized by domain so runtime behavior, UX behavior, and storage design can evolve independently.

## Runtime

- `runtime/codex_app_server_integration.md`
- `runtime/codex_app_server_integration_test_plan.md`
- `runtime/services_and_lifecycle.md`

## UX

- `ux/tele_cli_ux.md`
- `ux/full_ui_migration.md`
- `ux/full_ui_migration_test_plan.md`

## Storage

- `storage/storage_overview.md`
- `storage/artifact_contract.md`
- `storage/artifacts.md`
- `storage/retention_and_pruning.md`

## SQLite

- `migrations/sqlite/sqlite_migration.md`
- `migrations/sqlite/sqlite_schema.md`
- `migrations/sqlite/sqlite_trace_model.md`
- `migrations/sqlite/sqlite_operations.md`

## Notes

- `spec.md` remains the product-level overview.
- SQLite is the target durable store for runtime state, queues, message persistence, and trace history.
- Large raw bodies remain on disk as artifacts. SQLite stores metadata, references, and searchable previews.
- The current JSON-state to SQLite move is a hard cutover, not a backward-compatibility exercise.
