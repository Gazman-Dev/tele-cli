# AGENTS.md instructions for this workspace

## Workspace layout

- This root workspace maps to the direct 1:1 operator chat.
- Topic workspaces live under `topics/` and are isolated from each other.
- Durable shared memory lives in `long_memory.md` and this `AGENTS.md`.
- Temporary Tele Cli memory lives outside this workspace under `../memory/`.

## Git model

- This directory is the parent workspace repository.
- Each topic directory under `topics/` is its own Git repository.
- Tele Cli may register topics in the parent repo as submodule-style gitlinks.

## Constraints

- Keep changes scoped to the active workspace.
- Do not assume memory files under `../memory/` are durable or committed.
- If a dependency, tool, or script is missing, ask the operator before installing or scaffolding it globally.
