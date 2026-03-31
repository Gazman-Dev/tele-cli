# Codex App Server Integration Test Plan

## Status

- Status: active working test plan
- Date: March 31, 2026
- Parent spec: `spec/codex_app_server_integration_spec.md`

## Goal

Prove that Tele Cli can supervise Codex App Server, preserve session mappings, and recover explicitly when continuity is unsafe.

## Test Layers

### 1. Unit tests

Cover deterministic logic such as:

- JSON-RPC framing and request correlation
- session attachment and routing rules
- stale-turn detection
- instruction refresh decisions
- approval-store serialization

### 2. Integration tests with fakes

Run the runtime against:

- a fake app-server transport
- a fake Telegram client
- isolated temp state files
- controllable time where partial flush or stale-turn logic matters

These tests should verify behavior across component boundaries without a real Codex subprocess.

### 3. Subprocess-level smoke tests

Use a controlled fake `codex app-server` subprocess to prove:

- service boot can initialize the runtime
- a session can start a thread and complete a turn
- restart can reuse persisted thread mappings
- approval flow survives persistence boundaries

### 4. Recovery tests

Exercise explicit failure handling for:

- initialize failure
- child exit and restart backoff
- failed `thread_resume`
- stale active turn recovery
- stale approval after restart

## Harnesses Needed

### Fake App Server

It should:

- speak line-delimited JSON-RPC
- interleave notifications with responses
- simulate thread and turn success paths
- emit approval requests
- simulate resume failure and process exit

### Fake Telegram client

It should:

- capture outgoing messages and edits
- capture typing activity
- script inbound updates
- simulate duplicate delivery or retry conditions

### Temp state fixture

It should create isolated copies of:

- `runtime.json`
- `sessions.json`
- `approvals.json`
- `codex_server.json`
- `auth.json`
- `config.json`

### Time control

Needed for:

- idle partial flush behavior
- typing indicator cadence
- stale active turn detection

## Priority Scenarios

### First user message

- session exists or is created
- thread starts
- turn starts
- output is delivered and persisted

### Restart with persisted thread id

- runtime loads the session
- resume is attempted
- existing thread is reused when valid

### Failed resume

- session is not silently rebound
- degraded or explicit recovery state is recorded

### Mid-turn follow-up

- active turn is steered when appropriate
- session state remains consistent

### Approval request

- request is persisted
- operator can answer it
- turn proceeds or terminates accordingly

### Child exit

- Codex state moves out of healthy running state
- restart/backoff policy is exercised
- Telegram-facing service remains responsive when possible

## Done Criteria

This plan is satisfied when:

- session continuity and explicit degradation are both covered
- the app-server protocol edge cases are testable without the real network
- restart and resume behavior is exercised at least once above the unit-test layer
