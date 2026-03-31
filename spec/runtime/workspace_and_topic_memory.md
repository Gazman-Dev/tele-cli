# Workspace And Topic Memory Spec

## 1. Document Status

- Status: draft
- Date: March 31, 2026
- Related specs:
  - `spec/runtime/codex_app_server_integration.md`
  - `spec/runtime/services_and_lifecycle.md`
  - `spec/storage/storage_overview.md`
- Purpose: define how Tele Cli organizes operator workspaces so session routing, Git state, and long-lived topic memory stay predictable across devices and restarts

## 2. Problem

The current model keeps session continuity, but it does not give each conversation a stable filesystem workspace that matches how the operator actually works.

This causes several problems:

- Codex may need to rediscover which repo or folder it should operate in.
- Topic-specific context is mixed into generic session memory.
- Git history and topic history are not aligned to the same working directory.
- Group topics are logically separate workstreams, but they do not yet have first-class workspace boundaries.
- users naturally refer to Telegram topics by the visible topic name, while the current draft language drifted toward implementation words like "slug"

The result is weaker memory, weaker Git ergonomics, and more room for agent drift or hallucinated repo edits.

## 3. Goal

Tele Cli should treat filesystem workspace layout as part of the session contract.

Target outcome:

- the 1:1 operator chat is the canonical root workspace
- each Telegram group topic gets its own dedicated sub-workspace
- each topic workspace has its own Git repository connected through a parent recursive Git workflow
- each topic workspace becomes the Codex working directory for that topic
- each topic workspace has an `AGENT.md` file for topic-specific durable guidance
- general operator memory remains separate from topic-local memory

This is a structural change to how Tele Cli should think about sessions, not just a storage optimization.

## 4. Core Model

### 4.1 Canonical workspace root

Each Tele Cli installation already has one state root selected at setup time.

Within that root, Tele Cli should define one canonical operator workspace root.

That root workspace represents the main 1:1 chat with the operator.

Properties:

- exactly one root workspace per Tele Cli state directory
- this workspace is the default working directory for the direct operator chat
- it is the parent of all topic workspaces
- it is the natural place for cross-topic notes, shared scripts, and general operator context

### 4.2 Topic workspaces

Each Telegram group topic should map to its own subdirectory under the root workspace.

Properties:

- one topic workspace per `(chat_id, topic_id)` pair
- topic workspace path is deterministic and stable
- topic workspace is isolated from other topics
- Codex for that topic always starts from that topic workspace
- topic-specific memory lives inside that topic workspace

Terminology rule:

- this spec should use **topic** to mean the actual Telegram topic as the operator sees it in the Telegram UI
- implementation details such as filesystem-safe naming should stay secondary to that user-facing concept

### 4.3 Session versus workspace

A Tele Cli session and a workspace are related but not identical.

Rules:

- session is the conversation/runtime identity
- workspace is the filesystem and Git identity
- multiple sessions over time may reuse the same workspace
- creating a fresh session with `/new` must not silently create a different workspace unless the operator explicitly asks for that

This keeps conversational resets separate from filesystem resets.

## 5. Directory Layout

The design must stay portable and may not depend on developer-specific absolute paths.

Inside the configured Tele Cli state root, the recommended layout is:

```text
<tele-cli-root>/
  auth.json
  config.json
  tele_cli.db
  artifacts/
  memory/
    lessons/
    sessions/
      <session_id>.short_memory.md
  workspace/
    AGENT.md
    long_memory.md
    .gitignore
    .git/
    topics/
      <topic-folder>/
        AGENT.md
        .gitignore
        .git/
```

Notes:

- `workspace/` is the operator's main 1:1 chat workspace.
- `workspace/topics/` contains dedicated per-topic workspaces.
- the current global memory system remains in place, but its files move to the new structure:
  - `workspace/long_memory.md`
  - `memory/lessons/`
  - `memory/sessions/*.short_memory.md`
- everything under `memory/` is temporary working memory owned by Tele Cli
- `memory/` content does not need to be committed
- each topic directory name should be based on the visible Telegram topic name, but must still be backed by durable transport identity in storage
- the storage layer should persist workspace-relative paths, not machine-specific absolute paths

## 6. Workspace Identity Rules

### 6.1 Root workspace identity

The direct 1:1 chat is always bound to the root workspace.

It does not get a subfolder under `topics/`.

### 6.2 Topic workspace identity

From the operator perspective, the topic workspace corresponds to the Telegram topic by its visible topic name.

That means:

- if the user says "work on the Bayonne pump topic", the agent should understand that as the topic workspace for that Telegram topic
- the filesystem folder should also be named from that topic concept, not from an opaque internal slug term

At the storage level, Tele Cli must still bind that folder to durable transport identity.

Required stored identity inputs:

- Telegram `chat_id`
- Telegram `topic_id`
- current visible Telegram topic name

Recommended folder naming pattern:

- `<topic-name>`

If additional disambiguation is needed on disk, Tele Cli may append a stable suffix derived from `chat_id` and `topic_id`, but the primary name should stay recognizable as the Telegram topic the user sees.

### 6.3 Renames

If a Telegram topic title changes, the workspace should not move automatically.

Reason:

- path stability is more important than matching the latest title
- automatic folder moves would create avoidable Git and state churn

If a better human-readable name is needed later, it should be an explicit maintenance action.

## 7. Git Model

### 7.1 Root chat Git repository

The root workspace should be a Git repository.

Purpose:

- preserve the operator's main working history
- provide a stable base for 1:1 direct work
- allow structured long-lived notes and artifacts

### 7.2 Topic Git repositories

Each topic workspace should be its own Git repository.

This is intentionally not a single monorepo-with-folders design.

Reason:

- topics are separate workstreams
- independent Git history reduces accidental cross-topic contamination
- Codex can operate without guessing which repo is relevant
- per-topic commits, status, diffs, and recovery stay local to that topic

### 7.3 Submodule semantics

Topic workspaces should be managed as Git submodules under the root workspace.

Reason:

- the operator can clone or update the whole Tele Cli workspace tree with one recursive Git command
- each topic still keeps a separate repository and separate history
- switching devices becomes simpler because the topic repositories come along with the parent workspace model

Operational rule:

- the root workspace is the parent Git repository
- each topic workspace is a Git submodule entry under `workspace/topics/`
- Tele Cli should treat topic workspaces as independent repos for Codex operations, but as submodules for workspace synchronization and multi-device portability

### 7.4 Auto-init policy

When a topic workspace is created for the first time, Tele Cli should ensure:

- the directory exists
- an `AGENT.md` exists
- a `.gitignore` exists
- a Git repository exists and is registered as a submodule from the root workspace

If the repo or submodule wiring is missing, Tele Cli should initialize it automatically.

Required behavior:

- immediately after `git init`, Tele Cli should create an initial commit
- that initial commit should include the scaffolded files that define the workspace shape
- the root workspace should also get an initial commit after its Git initialization

Tele Cli should also attempt to push after these commits when a remote is configured and reachable.

Push behavior is best-effort:

- if push succeeds, Tele Cli continues normally
- if push fails, Tele Cli should continue locally and record the failure for operator visibility

### 7.5 Git ignore policy

Tele Cli should scaffold a reasonable `.gitignore` in the root workspace and in each topic workspace.

Minimum goals:

- keep Git metadata clean
- avoid committing temporary editor files, OS files, and Tele Cli temporary memory files
- preserve committed durable memory such as `long_memory.md` and `AGENT.md`

At minimum, the ignore file should cover:

- `.DS_Store`
- editor swap and temp files
- Python cache directories if they appear in the workspace
- any Tele Cli-generated temporary files that do not belong in repo history

The exact template can be standardized later, but the scaffold must be explicit and intentional.

## 8. Codex Working Directory Rules

This is the most important runtime rule in this spec.

### 8.1 Direct chat

For the 1:1 operator chat:

- Codex `cwd` must be the root workspace

### 8.2 Group topics

For a Telegram group topic:

- Codex `cwd` must be that topic's workspace directory

### 8.3 No repo search

Codex should not need to discover which repository it should use for a topic.

Tele Cli should provide the correct working root deterministically from routing metadata.

Implications:

- no "search the filesystem for the repo" behavior
- no relying on remembered previous shell state
- no ambiguity between unrelated topics

## 9. Memory Model

This design adds a topic-local durable memory layer alongside the existing Tele Cli memory system.

### 9.1 Existing global memory

The existing Tele Cli memory system remains active.

Today that includes:

- `workspace/long_memory.md`
- `memory/lessons/`
- per-session short memory under `memory/sessions/*.short_memory.md`

Those files remain part of the system-level memory model.

### 9.2 Root workspace memory

The root workspace `AGENT.md` stores long-lived guidance relevant to the main 1:1 chat and to Tele Cli operation broadly.

Examples:

- operator preferences
- shared local environment notes
- cross-topic workflow conventions
- stable repo or device facts that apply broadly

### 9.3 Topic memory

Each topic workspace `AGENT.md` stores durable guidance that matters only to that topic.

Examples:

- what this topic is about
- topic-specific goals or constraints
- repo layout notes for that topic
- known pitfalls for this workstream
- decisions the agent should remember between sessions

### 9.4 Separation rule

Topic-specific facts should not be pushed into the global `AGENT.md` unless they truly matter outside that topic.

The goal is:

- global memory stays short and broadly relevant
- topic memory stays local and high-signal

### 9.5 Relationship to existing memory

Instead:

- SQLite/session memory remains the runtime continuity layer
- `workspace/long_memory.md`, `memory/lessons/`, and session short memory remain the current Tele Cli-managed memory mechanism
- `AGENT.md` files add workspace-local durable context on top of that

Important retention rule:

- content under `memory/` is temporary and may be pruned or regenerated by Tele Cli
- `workspace/long_memory.md` is durable and should be committed
- `AGENT.md` files are durable and should be committed

## 10. `AGENT.md` Requirements

### 10.1 Required files

Tele Cli should ensure these files exist:

- root workspace: `workspace/AGENT.md`
- topic workspace: `workspace/topics/<topic>/AGENT.md`

### 10.2 Ownership

These files are Codex-managed workspace memory files.

Rules:

- Tele Cli must not parse, merge, or load `AGENT.md` itself
- Tele Cli is only responsible for ensuring the files exist at the correct paths
- Codex's native `AGENT.md` behavior is the intended mechanism for using them
- the operator may also edit them directly

### 10.3 Suggested content structure

Recommended sections:

- purpose
- overall system structure
- workspace layout
- topic/repository boundaries
- current goals
- important paths
- repo/worktree notes
- constraints
- known decisions
- follow-up items

The default template should explain the overall system structure and design clearly enough that Codex can understand how this workspace is intended to work.

### 10.4 Template ownership

Tele Cli should scaffold the initial `AGENT.md` template, but after that the file is still treated as Codex-managed content.

The template should describe:

- the overall Tele Cli workspace structure
- the separation between root chat and topic workspaces
- the Git/submodule model
- which files are durable versus temporary
- how existing Tele Cli memory files relate to `AGENT.md`

### 10.5 Prompt injection policy

Tele Cli does not define custom `AGENT.md` merge semantics.

Codex's native behavior should be used as-is.

For this spec, that means:

- Tele Cli ensures the root and topic `AGENT.md` files exist
- Codex handles combining them
- topic `AGENT.md` precedence and weighting are left to Codex native behavior

## 11. Session Routing Implications

The session router should resolve not only the session and thread, but also the workspace.

Required routing outputs:

- `session_id`
- `thread_id`
- `workspace_kind` such as `root` or `topic`
- `workspace_relpath`
- `agent_md_relpath`
- visible Telegram topic name where applicable

This should become part of the durable session/workspace mapping in storage.

## 12. Storage Implications

The storage model should persist workspace metadata explicitly.

At minimum, future runtime state should be able to answer:

- which workspace belongs to this session?
- which workspace belongs to this Telegram topic?
- what visible Telegram topic name is currently associated with that workspace?
- what is the relative path for that workspace?
- what is the relative path for its `AGENT.md`?
- what is the relative path for its `long_memory.md` where applicable?
- is the workspace initialized?
- is its Git repository initialized?
- is its Git submodule linkage initialized?

These should be stored as relative paths under the Tele Cli root.

## 13. UX Implications

The operator should not need to think about repo discovery anymore.

Expected behavior:

- direct chat always works in the root workspace
- each group topic always works in its own workspace
- `/new` starts a fresh conversation, not a fresh repo and not a fresh workspace
- if a topic is seen for the first time, Tele Cli prepares the workspace automatically

Possible future commands:

- `/where`
- `/topic-workspace`
- `/memory`

Those commands are not part of this spec yet, but the architecture should support them.

## 14. Migration Direction

This document is intentionally a design-first spec.

The implementation should not start until a migration plan is approved.

That later migration must cover:

- how the root workspace is created for existing installs
- how topic workspaces are named and initialized
- how current sessions gain workspace metadata
- how Codex thread creation starts using workspace-derived `cwd`
- how `AGENT.md` files are bootstrapped
- how `workspace/long_memory.md` is created or migrated
- how `memory/lessons/` and `memory/sessions/` move under the temporary-memory subtree
- how initial commits and best-effort pushes are triggered safely
## 15. Decisions

The following decisions are now fixed for implementation planning:

1. The direct-chat workspace lives under `workspace/`, not at the Tele Cli state root.
2. Topic workspaces live under `workspace/topics/`.
3. Git repositories receive an automatic initial commit immediately after initialization.
4. Tele Cli scaffolds an `AGENT.md` template that explains system structure, workspace layout, durable versus temporary memory, and the Git/submodule design.
5. Tele Cli should attempt a best-effort push after automatic commits whenever a remote is configured.
6. `workspace/long_memory.md` is durable and committed.
7. `memory/lessons/` and `memory/sessions/` are temporary and do not need to be committed.

## 16. Acceptance Criteria

This spec is satisfied only if the eventual implementation guarantees:

- the direct 1:1 chat always resolves to one deterministic root workspace
- each Telegram topic always resolves to one deterministic topic workspace
- topic workspaces are recognizable by the Telegram topic names users actually see
- topic workspaces are isolated from each other
- each topic workspace is its own Git repository and Git submodule
- the root workspace is also a Git repository
- each initialized workspace receives an initial commit automatically
- Tele Cli attempts a best-effort push after automatic commits when possible
- Codex `cwd` is resolved from the workspace mapping, not from repo search
- root and topic `AGENT.md` files exist and are addressable through stable relative paths
- `workspace/long_memory.md` exists as committed durable memory
- the existing Tele Cli memory files remain present and compatible with the new workspace model
- `memory/` remains disposable temporary storage and does not need to survive Git loss
- session resets do not silently move the user into a different workspace

## 17. Sleep Commit Policy

Sleep should produce Git-visible history for durable workspace knowledge.

Required behavior:

- after sleep updates durable workspace memory, Tele Cli should create commits for affected repositories
- if root workspace durable files changed, commit them in the root workspace repo
- if a topic workspace durable files changed, commit them in that topic repo

The primary expected durable file from the current memory system is:

- `workspace/long_memory.md`

Temporary files under `memory/` are excluded from this commit policy.

Push behavior after sleep:

- after creating sleep-driven commits, Tele Cli should attempt to push the affected repos
- push remains best-effort, not mandatory for local correctness
