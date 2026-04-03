# Spec Index

This directory is organized by area so runtime behavior, UX behavior, storage design, and migration work can evolve independently.

## Core

- `../spec.md`

## Runtime

- `codex_app_server_integration_spec.md`
- `codex_app_server_integration_test_plan.md`
- `services_and_lifecycle_spec.md`
- `workspace_and_topic_memory.md`

## UX

- `ux_spec.md`
- `full_ui_migration_spec.md`
- `full_ui_migration_test_plan.md`

## Storage

- `storage/storage_overview.md`
- `storage/logging_spec.md`

## SQLite Migration

- `migrations/sqlite/sqlite_migration.md`
- `migrations/sqlite/sqlite_schema.md`

## Notes

- `workspace/` is the direct-chat workspace.
- `workspace/topics/` holds topic workspaces.
- `workspace/long_memory.md` is durable committed memory.
- `memory/lessons/` and `memory/sessions/*.short_memory.md` are temporary Tele-CLI memory files.
- `AGENTS.md` files are Codex-managed workspace memory files that Tele-CLI only scaffolds.
