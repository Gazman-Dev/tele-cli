# Codex App Server Integration Spec

## 1. Document Status

- Status: draft
- Date: March 14, 2026
- Scope: replace the current single-child `codex` subprocess model with an always-on Codex App Server integration, while moving session routing and operator auth into Tele Cli

## 2. Goal

Tele Cli should stop treating Codex as a single interactive terminal child and instead treat Codex App Server as the long-lived agent runtime.

Target outcome:

- one always-on Tele Cli service
- one long-lived Codex App Server process managed by Tele Cli
- Tele Cli owns operator auth and message-to-session routing
- Codex owns thread lifecycle, turn execution, approvals, config, and native account login
- Telegram messages can be routed to the correct Codex thread without losing continuity when the UI disconnects

## 3. Why App Server

Based on OpenAI’s February 4, 2026 engineering post, App Server is now the first-class Codex integration surface and is the same harness used across Codex surfaces. It exposes:

- a long-lived process hosting multiple Codex threads
- bidirectional JSON-RPC
- durable thread lifecycle primitives: initialize, thread, turn, item
- reconnect-friendly streaming updates and approval requests
- built-in auth/config/model discovery support

This is a better fit than the current design in this repo, which assumes:

- one service instance
- one Codex child process
- one active Codex session

That current assumption appears in `README.md`, `docs/wiki/Architecture.md`, and the runtime modules under `src/runtime`.

## 4. Research Summary

### 4.1 Official OpenAI findings

From OpenAI’s post "Unlocking the Codex harness: how we built the App Server" published February 4, 2026:

- App Server is the recommended long-term integration method.
- It is a long-lived process hosting Codex core threads.
- The protocol is bidirectional JSON-RPC over JSONL/stdin-stdout.
- A single client request can produce many server notifications.
- The server can initiate approval requests and pause execution until the client responds.
- Threads are durable and can be created, resumed, and archived.
- Web-style deployments keep task state server-side so work continues if the client disconnects.

### 4.2 Local CLI findings from this machine

Validated locally on March 14, 2026 using `codex --help` and `codex app-server --help`:

- `codex app-server` is installed locally.
- It supports `--listen stdio://` by default and `--listen ws://IP:PORT`.
- It can generate TypeScript bindings and JSON Schema for the protocol.
- The app-server command is marked experimental.

### 4.3 Local schema findings from this machine

Generated protocol schema inspection shows:

- `thread/start` sets thread-level defaults such as model, sandbox, approval policy, cwd, instructions, and personality.
- `turn/start` sends new user input into an existing thread via `threadId`.
- `thread/resume` resumes a durable thread by `threadId`.
- `turn/steer` supports steering an in-flight turn with additional user input.
- `turn/interrupt` supports explicitly halting an in-flight turn.
- `login/account` supports `apiKey` and `chatgpt` login types.
- `getAccount` exposes whether OpenAI auth is required and the current account type.
- `thread/metadata/update` currently supports Git metadata patching, not arbitrary Tele Cli metadata.

Implication: Tele Cli should keep its own routing table and only store Codex-native identifiers inside it.

## 5. Recommendation

Adopt a split-responsibility design:

- Tele Cli becomes the control plane.
- Codex App Server becomes the agent runtime.

Tele Cli should own:

- Telegram pairing and authorization
- mapping incoming external messages to a Tele Cli session
- mapping each Tele Cli session to a Codex `threadId`
- policy for when to create, reuse, detach, or retire a Codex thread from Telegram visibility
- persistence of operator/session metadata
- service lifecycle, health checks, reconnects, and observability

Codex should own:

- thread persistence and resume
- turn execution
- streaming output and item lifecycle
- tool execution and approvals
- model/auth/config discovery
- Codex-native thread history

## 6. Target Architecture

```text
Telegram/User Surface
        |
        v
Tele Cli Service
  - auth gate
  - session router
  - app-server client
  - approval broker
  - state store
        |
        v
Codex App Server
  - initialize
  - thread manager
  - turn execution
  - item stream
  - native auth/config/model APIs
```

### 6.1 Core runtime model

- one Tele Cli service process
- one Codex App Server child process per host
- many Tele Cli sessions
- one Codex thread per active Tele Cli session
- zero or more active turns across those threads, depending on concurrency policy

### 6.2 Session model

Add a Tele Cli session abstraction that is separate from Codex threads.

Suggested fields:

- `session_id`
- `transport` such as `telegram`
- `transport_user_id`
- `transport_chat_id`
- `thread_id`
- `status`
- `created_at`
- `last_user_message_at`
- `last_agent_message_at`
- `last_turn_id` if we choose to track it
- `pending_approval_id`
- `title`
- `tags`

Suggested rule:

- Tele Cli is the source of truth for who a user is and which external conversation maps to which Codex thread.
- Codex is the source of truth for what happened inside the thread.
- at any moment, a Telegram chat or group topic has exactly one implicit active session mapping

### 6.3 Workspace binding

Session routing should also resolve a deterministic workspace, not only a `thread_id`.

Direction:

- the direct 1:1 operator chat binds to the root workspace
- each Telegram group topic binds to its own dedicated topic workspace
- Codex `cwd` for a turn should come from that workspace mapping

See `spec/workspace_and_topic_memory.md` for the filesystem, Git, and topic-memory contract.

## 7. State Design

Replace the current single-session runtime state with separate stores.

### 7.1 Proposed files

- `auth.json`
  - existing Telegram/operator auth
- `runtime.json`
  - high-level service and app-server health only
- `sessions.json`
  - Tele Cli session registry and thread mapping
- `approvals.json`
  - pending approval requests from Codex, keyed by request id
- `codex_server.json`
  - app-server pid, transport, protocol version, initialized state, account state

Workspace and memory companion files remain outside runtime JSON state:

- `workspace/long_memory.md`
- `memory/lessons/`
- `memory/sessions/*.short_memory.md`
- root and topic `AGENTS.md` files under `workspace/`

### 7.2 Session routing rules

Default routing for Telegram:

- one authorized Telegram chat or group topic maps to one implicit active Tele Cli session
- additional sessions may be created on demand
- older sessions may exist in persisted history, but only one session is attached to Telegram for a given chat or topic at a time
- when a user starts a new session, that session replaces the current implicit session for that chat or topic and the prior session becomes detached from Telegram
- a detached session may continue running in the background until its turn finishes
- once a detached session is idle and has no buffered output left, Tele Cli may prune it automatically
- Telegram does not expose manual session reactivation because that creates avoidable routing collisions
- replies without an explicit session selector route to the current implicit session for that chat or topic

Suggested first commands:

- `/new`
- `/sessions`
- `/status`

Danger-mode default for Telegram-created threads:

- Tele Cli starts new Codex threads with `sandbox = "danger-full-access"`
- Tele Cli starts new Codex threads with `approvalPolicy = "never"`
- approval handling remains a compatibility path if the app server emits an approval request anyway

## 8. Protocol Integration Plan

### 8.1 Transport choice

Phase 1 should use `stdio://`.

Reason:

- matches OpenAI’s recommended local-app pattern
- simplest process supervision model
- no extra socket security surface
- easiest to keep within the current local service architecture

`ws://` should remain an optional future mode for remote UI attachment or cross-host operation, not the first rollout target.

### 8.2 Handshake

On service start:

1. launch `codex app-server --listen stdio://`
2. perform `initialize`
3. read server capabilities and protocol version
4. query account state
5. warm local caches for models, collaboration modes, and config requirements if needed

### 8.3 Thread lifecycle

For each Tele Cli session:

1. if a `thread_id` exists, call `thread/resume`
2. otherwise call `thread/start`
3. persist the returned `thread_id`
4. keep reading the shared app-server event stream and route notifications by `threadId` and `turnId`

### 8.4 Turn execution

For each incoming user message:

1. resolve Tele Cli `session_id`
2. resolve mapped Codex `thread_id`
3. if no turn is active, send `turn/start` with text input
4. if a turn is already active for that session, send `turn/steer` with the new user input by default
5. buffer item and turn notifications in Tele Cli
6. finalize Tele Cli session timestamps and status on `turn/completed`

### 8.5 Stop behavior

Tele Cli should expose an explicit stop command for dangerous or unwanted agent behavior.

Policy:

- `/stop` maps to Codex `turn/interrupt` for the active turn in the current chat/topic session
- if no turn is active, `/stop` should return a clear no-op message
- interrupted turns should be marked distinctly in Tele Cli session state so the operator can decide whether to continue, steer again, or start a new session
- after a successful interrupt, Tele Cli should send an explicit short stopped marker to Telegram

### 8.6 Telegram delivery policy

Telegram delivery should default to non-streaming final answers.

Required behavior:

- when a turn starts, Tele Cli should set Telegram typing state for the target chat/topic
- while the turn is active, typing indicators should be refreshed on a timer as needed by Telegram semantics
- Tele Cli should not send token-by-token or delta-by-delta content to Telegram by default
- when the turn completes, Tele Cli should send the final answer as a Telegram message
- if a turn is interrupted, Tele Cli should send a short interruption confirmation instead of pretending the turn completed normally

### 8.7 Partial-response pause flushing

We still need to handle long pauses after meaningful partial output.

Policy:

- Tele Cli buffers only user-visible assistant text derived from assistant message delta events
- if assistant-visible output has advanced and then no new assistant-visible output arrives for more than the configured idle threshold, default 3 seconds, Tele Cli flushes the buffered partial answer to Telegram as a message
- if output resumes later, including after a `turn/steer`, Tele Cli starts a new buffer and may flush again after another 3-second idle gap
- the final completion sends the remaining buffered output
- partial flushing should pause while the turn is blocked on an approval request

Purpose:

- preserve responsiveness without noisy token streaming
- give the user progress if Codex pauses mid-answer or mid-tool sequence
- keep Telegram output chunked at natural pause boundaries instead of raw deltas

### 8.8 Approval flow

Codex can issue server requests for approval. Tele Cli needs an approval broker:

1. receive approval request from app server
2. persist it in `approvals.json`
3. notify the operator via Telegram/app shell UI
4. accept allow/deny input
5. answer the pending JSON-RPC request

This is mandatory. Without it, turns can stall indefinitely.

## 9. Auth Strategy

There are two distinct auth domains and they should stay separate.

### 9.1 Tele Cli auth

Tele Cli auth remains the gate for who may control the service:

- Telegram bot token validation
- pairing authorized operator/chat
- optional future multi-operator policy

### 9.2 Codex/OpenAI auth

Codex App Server manages OpenAI account auth natively.

Based on local schema, it supports at least:

- API key login
- ChatGPT login

Recommended design:

- Tele Cli should detect Codex auth state through app-server APIs on boot
- Tele Cli should not store raw OpenAI credentials unless required for explicit API-key bootstrap
- Tele Cli should support both `chatgpt` login and `apiKey` login
- phase 1 should start with `chatgpt` login first
- when Codex indicates login is required, Tele Cli should call the app-server login start flow for `chatgpt`, obtain the returned auth URL, and send that link to Telegram so the user can complete auth there
- Tele Cli should then wait for the corresponding login completion and account-updated notifications before marking Codex auth ready
- the localhost OAuth callback remains app-server-owned; Tele Cli only brokers the URL and tracks completion state
- API key login should remain supported as a later setup path using the same account-state detection flow

## 10. Concurrency Policy

Start conservative.

Phase 1 policy:

- many durable sessions
- one active turn per session
- additional user input for that same session is handled via `turn/steer`

Reason:

- keeps Telegram UX predictable
- reduces approval interleaving complexity
- simplifies output buffering and retry handling
- gives us a stable migration path from the current single-session runtime

Clarification:

- a single session should not have two separate overlapping turns at the same time
- if a second user message arrives for the same chat/topic while the current turn is still running, Tele Cli should use `turn/steer` by default rather than queueing or rejecting it
- the separate global concurrency cap controls how many different sessions may run at once across the whole service

Recommended v1:

- enforce exactly one active turn per session
- use `turn/steer` as the default behavior for mid-turn user follow-ups
- expose `/stop` as a hard interrupt mapped to `turn/interrupt`
- allow different sessions to run independently
- revisit stricter service-wide throttling only if real Telegram delivery or reliability issues require it

## 11. Failure Model

Tele Cli must explicitly handle:

- app-server process crash
- initialize failure
- Codex auth expired
- thread resume failure for a stored `thread_id`
- approval request lost during service restart
- duplicate incoming Telegram deliveries
- Telegram reconnect while Codex turn is still running
- service restart while sessions are active

Required behavior:

- app-server child is supervised and restarted
- session-to-thread mapping survives service restart
- orphaned pending approvals are marked stale and surfaced to the operator
- if `thread/resume` fails, Tele Cli moves the session to `degraded` and requires explicit recovery instead of silently starting a new thread

## 12. Migration From Current Code

Current code paths that will need major refactoring:

- `src/runtime/codex_runtime.py`
  - today this models a single subprocess terminal session
- `src/runtime/service.py`
  - today this assumes one active Codex child and streams raw output
- `src/runtime/control.py`
  - today this handles lock ownership for a single child pid
- `src/core/models.py`
  - runtime models are single-session oriented

New modules likely needed:

- `src/runtime/app_server_client.py`
- `src/runtime/jsonrpc.py`
- `src/runtime/session_store.py`
- `src/runtime/approval_store.py`
- `src/runtime/session_router.py`
- `src/runtime/codex_session_manager.py`

## 13. Execution Plan

### Phase 0: design freeze

- approve this architecture
- choose first supported transport surface: Telegram only
- finalize Telegram login-link UX for ChatGPT auth
- finalize steer semantics for messages arriving during an active turn
- finalize `/stop` interrupt UX and messaging

### Phase 1: app-server spine

- add app-server supervisor and JSON-RPC transport
- implement `initialize`
- implement account-state read
- implement ChatGPT login-link flow and Telegram delivery for the login URL
- implement health model in `runtime.json`
- add structured app-server logging

Exit criteria:

- Tele Cli can start and keep an app-server child alive
- service can reconnect after child restart

### Phase 2: thread-backed sessions

- introduce `sessions.json`
- create Tele Cli session abstraction
- implement `thread/start` and `thread/resume`
- map one Telegram chat/topic to one implicit durable Codex thread
- add `/new` so a user can replace the implicit session with a newly created one
- detach the prior session automatically so Telegram keeps exactly one attached writable session per chat/topic
- allow detached sessions to finish in the background without surfacing replies into the new Telegram session

Exit criteria:

- restart service and continue the same thread
- no raw subprocess terminal dependency remains for normal flow

### Phase 3: turn routing and streaming

- replace raw `codex.send(text)` flow with `turn/start`
- implement `turn/steer` for mid-turn user follow-ups
- implement `/stop` mapped to `turn/interrupt`
- consume item/turn notifications
- implement Telegram typing mode while a turn is active
- implement final-answer delivery
- implement 3-second pause-based partial flushes
- persist last turn state

Exit criteria:

- end-to-end ask/reply works through app server
- Telegram receives final answers
- Telegram receives partial flushes only after the configured idle gap

### Phase 4: approval broker

- persist pending approvals
- expose operator commands for allow/deny
- reply to server requests

Exit criteria:

- Codex permission prompts can be completed remotely

### Phase 5: auth UX and recovery

- add Codex account setup/status flow
- detect expired/missing OpenAI auth
- add recovery actions for stale sessions, dead threads, and failed resumes

Exit criteria:

- first-run and restart behavior are operationally safe

### Phase 6: Telegram-safe session UX

- add `/new`, `/sessions`, and `/status`
- allow multiple durable sessions in storage per operator/chat, but only one writable current session in Telegram
- keep exactly one implicit active session per Telegram chat or group topic
- do not expose manual session switching in Telegram
- use detached-session semantics when `/new` is used during an in-flight turn
- add thread title/session title synchronization rules for diagnostics only

Exit criteria:

- operator can start a fresh conversation without risking session collisions

## 14. Key Risks

### 14.1 Experimental surface

The local CLI still marks `app-server` as experimental on March 14, 2026. We should assume protocol drift is still possible.

Mitigation:

- record the observed Codex CLI version on service boot
- record protocol version during initialize
- record initialize capabilities that affect client behavior
- fail fast if the protocol is incompatible with the client implementation
- surface upgrade-related breakage clearly in runtime status and logs
- add a compatibility check on service boot

### 14.2 Telegram is a poor fit for dense approval/event streams

App Server produces rich, high-frequency events. Telegram can become noisy or ambiguous.

Mitigation:

- aggregate deltas before sending
- use typing indicators while work is active
- send final answers by default
- send intermediate messages only after meaningful idle gaps
- expose verbose event streaming only in internal diagnostics, not the main app shell

### 14.3 Session/thread divergence

Tele Cli session routing and Codex thread persistence can drift apart.

Mitigation:

- never infer routing from Codex alone
- persist session-to-thread mapping transactionally
- make recovery explicit when resume fails

## 15. Decisions Needed

Before implementation, decide:

1. For pause-based partial flushes, should the 3-second threshold be configurable in `config.json` or hard-coded initially?
2. Should detached background sessions remain visible in `/sessions`, or only appear in local diagnostics and logs?

## 16. Proposed First Implementation Cut

I recommend the smallest viable integration be:

- `stdio://` transport
- floating Codex CLI upgrades
- one always-on app-server child
- ChatGPT login-link flow via Telegram first, with API key auth added as a second path
- one implicit session per authorized Telegram chat or group topic
- `/new` support so the implicit session can be replaced on demand without manual session switching
- dangerous-mode defaults for Telegram-created threads: `danger-full-access` plus `approvalPolicy = never`
- durable `sessions.json`
- `thread/start`, `thread/resume`, `turn/start`, `turn/steer`, `turn/interrupt`
- Telegram typing mode during active turns
- final-answer delivery to Telegram
- 3-second pause-based partial flushes
- approval broker implemented before broader rollout

This keeps the operator UX simple while enforcing the Telegram rule that each chat or topic has exactly one writable current session.

## 17. Source Notes

Primary sources used for this spec:

- OpenAI engineering post: https://openai.com/index/unlocking-the-codex-harness/
- OpenAI developer model reference for Codex models: https://developers.openai.com/api/docs/models/gpt-5.3-codex
- Local CLI inspection on this machine via `codex --help` and `codex app-server --help`
- Local app-server JSON schema generated from the installed `codex` binary on March 14, 2026

Inference note:

- The split between Tele Cli-owned session routing and Codex-owned thread execution is an architectural recommendation inferred from the official App Server lifecycle plus the current Tele Cli codebase shape. It is not stated as a direct OpenAI requirement.
