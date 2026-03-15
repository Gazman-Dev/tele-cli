# Codex App Server Integration Test Plan

## 1. Document Status

- Status: draft
- Date: March 14, 2026
- Parent spec: [spec/codex_app_server_integration_spec.md](/C:/git/MiniC/spec/codex_app_server_integration_spec.md)
- Purpose: turn the integration spec into a testing-first implementation backlog with concrete test coverage targets

## 2. Approach

This project should not start by rewriting the runtime blindly. The first implementation cut should establish test seams around the parts of the system that currently assume:

- one Codex subprocess
- one active session
- one raw text stream to Telegram

The initial goal is to make the runtime replaceable without losing restart safety, routing correctness, or operator control.

Recommended order:

1. Add test harnesses and fake transports.
2. Add persistence models and stores with unit tests.
3. Add app-server client and supervisor with protocol-level tests.
4. Rework service orchestration behind those seams.
5. Add command and Telegram behavior tests before expanding session-history features.

## 3. Test Strategy

We should cover three layers.

### 3.1 Unit tests

Focus on deterministic logic with no real subprocesses or network:

- JSON-RPC framing and request correlation
- session routing rules
- approval persistence
- runtime state transitions
- partial-flush timing logic
- duplicate-delivery protection

### 3.2 Integration tests

Run the service against in-process fakes:

- fake app-server transport
- fake Telegram client
- temp-file state directory
- controllable clock/timer hooks where needed

These tests should verify persistence, recovery, and event routing across components.

### 3.3 End-to-end smoke tests

Use a controlled fake `codex app-server` process launched as a subprocess fixture. These tests should stay narrow and prove:

- service boot initializes correctly
- one session can create a thread and complete a turn
- restart resumes the same thread mapping
- approval flow unblocks a waiting turn

### 3.4 Lifecycle and host-integration tests

The always-on requirement depends on more than the app-server protocol. We also need tests around service ownership, restart behavior, and install/update flows.

Recommended strategy:

- test host-service behavior behind a `service_manager` adapter with fakes first
- keep real OS-specific checks limited to a small smoke layer
- treat duplicate-owner prevention and reboot/update recovery as first-class test areas, not implementation details

## 4. Harnesses To Build First

Before refactoring the runtime, add the following test utilities.

### 4.1 Fake App Server

Requirements:

- accepts line-delimited JSON-RPC over stdin/stdout
- can emit notifications interleaved with request responses
- can initiate approval requests toward the client
- can simulate process exit, initialize failure, and resume failure
- records received methods and params for assertions

Suggested location:

- `tests/fakes/fake_app_server.py`

### 4.2 Fake Telegram Client

Requirements:

- records outbound messages
- records typing-indicator calls
- serves scripted inbound updates
- allows duplicate update delivery scenarios

Suggested location:

- `tests/fakes/fake_telegram.py`

### 4.3 Temp State Fixture

Requirements:

- creates isolated `auth.json`, `runtime.json`, `sessions.json`, `approvals.json`, `codex_server.json`
- seeds auth/session data as needed
- exposes helpers to reload state files for assertions

### 4.4 Clock/Timer Control

Requirements:

- deterministic tests for idle partial flushes
- deterministic tests for typing refresh cadence
- deterministic tests for stale approval detection

If we do not inject time dependencies, the pause-flush behavior will be hard to verify safely.

### 4.5 Fake Service Manager

Requirements:

- simulates service install, enable, start, stop, restart, disable, and status
- records the canonical registered executable and state directory
- can simulate duplicate registrations and rapid consecutive installs
- can simulate boot-time auto-start and crash-time restart behavior

Suggested location:

- `tests/fakes/fake_service_manager.py`

### 4.6 Process Ownership Fixture

Requirements:

- simulates a live Tele Cli owner, stale owner, orphaned Codex child, and unknown ownership
- allows deterministic lock-conflict and split-brain scenarios
- records which process was kept, stopped, or marked ambiguous

## 5. Implementation Backlog

This is the recommended delivery order for code and tests.

### 5.1 Phase A: Test seam extraction

Tasks:

- isolate Telegram operations behind an interface used by `service.py`
- isolate Codex transport/process management behind an app-server client abstraction
- isolate state persistence behind dedicated store modules instead of ad hoc file writes
- isolate host service registration and lifecycle operations behind a service-manager abstraction
- isolate lock/process inspection behind injectable ownership probes
- inject clock/sleep dependencies into long-running orchestration paths

Tests:

- current service loop can run one iteration against fake Telegram input
- runtime state writes can be asserted without running real sleeps
- service startup path can run against fake service-manager and fake ownership inputs

### 5.2 Phase B: Data model expansion

Tasks:

- add models for session records, approval records, and app-server runtime state
- add `sessions.json`, `approvals.json`, and `codex_server.json` stores
- preserve existing auth pairing behavior while separating it from Codex account state

Tests:

- store round-trip serialization for new models
- updates preserve unrelated state entries
- stale/missing files load into safe defaults
- runtime state can represent degraded, backoff, and restart-needed lifecycle states explicitly

### 5.3 Phase C: JSON-RPC and supervisor spine

Tasks:

- implement JSON-RPC request/response transport
- launch and supervise `codex app-server --listen stdio://`
- perform `initialize` and account-state reads on boot
- persist app-server protocol and health metadata

Tests:

- request ids correlate correctly under interleaved notifications
- initialize success records protocol version and capabilities
- initialize failure marks degraded state
- child process exit triggers restart path
- repeated child failures enter bounded backoff without taking Telegram down

### 5.4 Phase D: Thread-backed sessions

Tasks:

- add session router
- implement `thread/start` and `thread/resume`
- map one Telegram chat/topic to one implicit session
- add `/new` session creation

Tests:

- first user message creates a session and thread mapping
- restart reuses stored `thread_id` via `thread/resume`
- resume failure marks session degraded instead of silently replacing the thread
- `/new` creates a new session and replaces the implicit mapping

### 5.5 Phase E: Turn lifecycle and delivery

Tasks:

- implement `turn/start`
- implement `turn/steer` for mid-turn follow-ups
- implement `/stop` mapped to `turn/interrupt`
- buffer assistant-visible output
- add typing indicators and idle-gap partial flushes

Tests:

- first message sends `turn/start`
- second message during active turn sends `turn/steer`
- `/stop` during active turn sends `turn/interrupt`
- `/stop` without active turn returns a no-op message
- final answer is delivered once on `turn/completed`
- 3-second idle gap flushes partial output once
- approval wait suppresses partial flushing while blocked

### 5.6 Phase F: Approval broker

Tasks:

- persist incoming approval requests
- route allow/deny replies from Telegram commands
- answer the pending JSON-RPC server request
- mark orphaned approvals stale after restart

Tests:

- approval request is stored and surfaced to the operator
- allow sends the correct JSON-RPC response
- deny sends the correct JSON-RPC response
- restart preserves pending approval state
- stale approval is not auto-answered after process loss

### 5.7 Phase G: Recovery and session-safety commands

Tasks:

- add `/sessions`, `/new`, and `/status`
- track exactly one writable implicit active session per chat/topic
- archive prior sessions automatically when `/new` replaces the current one
- keep archived sessions read-only from Telegram
- expose degraded and interrupted session states
- handle duplicate Telegram deliveries and restart races

Tests:

- `/sessions` lists current and archived sessions
- `/new` replaces the implicit mapping and archives the prior session
- archived sessions are never reactivated by Telegram commands
- duplicate Telegram update does not start duplicate turns
- service restart during active turn resumes safely without duplicating the final message

### 5.8 Phase H: Lifecycle, installer, and ownership recovery

Tasks:

- implement a service-manager adapter for install, update, restart, enable, disable, and status
- detect duplicate service registrations for the same state directory
- strengthen `app.lock` ownership metadata and conflict classification
- add repair flows for stale locks, orphaned Codex children, and duplicate registrations
- define update/migration execution with explicit preflight and rollback-safe behavior

Tests:

- second install updates or repairs the canonical service instead of creating a duplicate
- duplicate service registration is detected and surfaced for repair
- stale `app.lock` with dead owner is healed safely
- stale `app.lock` with orphaned Codex child is resolved according to policy
- live conflicting owner causes background startup to exit without split-brain takeover
- unknown ownership is marked degraded instead of auto-killed
- simulated host reboot auto-starts the service and restores Telegram polling
- simulated Tele Cli crash triggers service-manager restart
- simulated update preserves state and restarts onto the new code
- failed state migration leaves prior durable state recoverable

## 6. Detailed Test Cases

### 6.1 App Server Boot

- boot starts the app-server child exactly once
- successful `initialize` writes initialized state to `codex_server.json`
- `getAccount` result is persisted and exposed to runtime status
- unsupported protocol version fails fast with a clear runtime error state
- child crash after initialize transitions runtime to restarting and retries launch

### 6.2 Auth Separation

- Telegram pairing remains required before operator commands are honored
- missing Codex account auth triggers login-link flow rather than raw turn execution
- login URL is sent to the paired Telegram chat
- account-updated event clears login-pending state
- Telegram auth reset does not erase Codex server metadata unintentionally

### 6.3 Session Store and Routing

- first authorized message in a chat creates the implicit session
- later messages in the same chat reuse that session by default
- different chat id creates a different session
- group topic routing keys on chat/topic identity, not only chat id
- degraded session cannot auto-start a replacement thread without explicit recovery

### 6.4 Thread Lifecycle

- new session uses `thread/start` with configured defaults
- stored `thread_id` uses `thread/resume`
- resume success keeps existing session metadata intact
- resume failure sets session status to `degraded`

### 6.5 Turn Lifecycle

- idle session message sends `turn/start`
- active session follow-up sends `turn/steer`
- second follow-up during the same active turn is still routed to the same turn
- completed turn clears active-turn tracking in session state
- interrupted turn is marked distinctly from completed and failed turns

### 6.6 Telegram Delivery

- turn start emits typing indicator
- typing indicator refreshes on schedule while active
- no delta-by-delta messages are sent during active generation by default
- final completion sends the remaining buffered answer once
- interruption sends a short stopped marker instead of a normal final answer
- partial flushes split only on idle gaps, not every delta

### 6.7 Approval Flow

- approval request pauses Telegram partial flush behavior
- approval notification includes enough context for allow/deny action
- allow resumes the blocked turn
- deny resumes the blocked turn with a denial outcome
- unknown approval command returns an operator-visible error without corrupting stored state

### 6.8 Failure Handling

- app-server crash during active turn does not silently mark the turn completed
- restart with pending approval marks that approval stale
- malformed notification is logged and ignored without killing the service
- duplicate Telegram delivery is detected via update id and ignored
- failed state-file write surfaces degraded runtime status instead of silently continuing

### 6.9 Degraded Operation Guarantees

- Telegram polling starts even when Codex auth is missing
- Telegram remains available for `/status` while Codex is in restart backoff
- login-link delivery still works when Codex is `AUTH_REQUIRED`
- repeated Codex restart failures do not stop Telegram polling
- Telegram reconnect failure does not erase Codex/session state

### 6.10 Ownership And Duplicate-Service Handling

- background startup exits when another healthy owner already holds the same state directory
- foreground debug detects the background owner and requires explicit takeover flow
- duplicate service registration is detected before a second service is started
- installer repair keeps the canonical registration and removes the duplicate only after confirmation
- killing the wrong process still converges back to one owner after recovery

### 6.11 Update And Reboot Recovery

- reboot while idle restores Telegram polling and Codex supervisor automatically
- reboot during active turn preserves the original `thread_id` and marks the session interrupted or degraded
- update while idle preserves auth and session state
- update during active turn does not mark the turn completed without confirmation from Codex
- migration step is idempotent across repeated installer runs
- failed migration blocks startup cleanly and preserves recoverable prior state

## 7. First Tests To Write

These should be written before major runtime edits because they define the contract for the refactor.

1. `session_store` round-trip and implicit-session routing tests.
2. JSON-RPC transport test for interleaved notifications and responses.
3. service ownership test: second owner for the same state directory exits or degrades without split-brain takeover.
4. degraded-operation test: Telegram `/status` remains available while Codex is unavailable or auth-blocked.
5. service-level integration test: inbound Telegram message creates session, starts thread, starts turn, and sends final Telegram reply.
6. service restart test: persisted session resumes the same `thread_id`.
7. approval flow test: approval request persists, operator allows, turn completes.
8. pause-flush timing test: assistant delta output flushes after 3 seconds of idle time and not before.
9. simulated reboot test: service auto-start restores Telegram polling and Codex supervision.
10. duplicate-install repair test: installer/service-manager logic preserves one canonical service registration.

## 8. Expected Test File Layout

Suggested additions:

- `tests/test_jsonrpc.py`
- `tests/test_app_server_client.py`
- `tests/test_session_store.py`
- `tests/test_approval_store.py`
- `tests/test_session_router.py`
- `tests/test_service_app_server_flow.py`
- `tests/test_service_lifecycle.py`
- `tests/test_service_ownership.py`
- `tests/test_installer_lifecycle.py`
- `tests/test_state_migration.py`
- `tests/fakes/fake_app_server.py`
- `tests/fakes/fake_telegram.py`
- `tests/fakes/fake_service_manager.py`

Existing files likely to change:

- `tests/test_runtime.py`

That file currently covers the single-session runtime. It should be narrowed to generic runtime invariants while the new app-server behavior moves into dedicated tests.

## 9. Exit Criteria For The Testing-First Cut

Before broad implementation starts, we should have:

- fake app-server and fake Telegram fixtures in place
- fake service-manager and ownership fixtures in place
- deterministic service-loop integration tests
- explicit tests for session persistence and restart behavior
- explicit tests for approval persistence and resume behavior
- explicit tests for pause-based partial flush delivery
- explicit tests for one-owner-per-state-directory enforcement
- explicit tests for reboot, update, and duplicate-install recovery

Without those seams, the refactor will likely regress restart safety and chat-to-thread routing.
