# Full UI Migration Test Plan

## Status

- Status: active working test plan
- Date: March 31, 2026
- Parent spec: `spec/full_ui_migration_spec.md`

## Goal

Prove that Tele Cli's interactive local behavior now converges on one app shell instead of multiple unrelated flows.

## Test Strategy

We should cover the migration at four layers.

### 1. Entry-point tests

Verify that interactive runs of:

- `tele-cli`
- `tele-cli menu`
- `tele-cli setup`
- `tele-cli update`
- `tele-cli uninstall`

all enter the app shell, with the correct startup context.

### 2. Shell state tests

Verify that the shell renders the correct state and menu options for:

- first install
- incomplete setup
- healthy installed state
- Codex login required
- Telegram not paired
- duplicate registration or lock conflict

### 3. Shell action tests

Verify that shell actions invoke the correct backend operations for:

- dependency repair
- token validation
- pairing completion
- update
- uninstall
- duplicate registration repair

### 4. Narrow interactive smoke tests

Keep a small manual or pseudo-interactive suite that proves:

- splash appears
- setup screens appear when needed
- update flow stays inside the shell
- uninstall confirmation stays inside the shell
- completed actions return to the main status view

## Harnesses Needed

The shell tests should rely on fakes for:

- backend actions
- service manager analysis
- lock and setup conflict inspection
- Telegram validation and pairing
- runtime and Codex state loading
- shell input/output driving

These tests should avoid depending on a real Telegram bot or real service registration.

## Priority Scenarios

### First launch with no setup

- shell opens
- status resolves to configuration required
- operator is routed into setup

### Interactive `tele-cli setup`

- shell opens in setup context
- first incomplete step is shown
- completion returns to home

### Interactive `tele-cli update`

- shell opens in update context
- backend update is invoked
- progress and result stay inside the shell

### Interactive `tele-cli uninstall`

- shell opens in uninstall context
- confirmation is required
- uninstall action is invoked only after confirmation

### Duplicate registration found

- shell shows the conflict
- repair action can be chosen
- shell returns to healthy or still-degraded state explicitly

### Codex login required

- shell surfaces login-required status clearly
- shell remains usable as the control plane

## Done Criteria

This plan is satisfied when:

- interactive entry-point coverage is automated
- shell state transitions are testable without real terminal timing
- operator-facing recovery and maintenance flows are exercised through the shell
