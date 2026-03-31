# Full UI Migration Spec

## Status

- Status: active working spec
- Date: March 31, 2026
- Related docs:
  - `spec.md`
  - `spec/ux_spec.md`
  - `spec/services_and_lifecycle_spec.md`

## Goal

Interactive local use should converge on one real app shell.

The product should stop behaving like a mixture of:

- installer prompts
- ad hoc maintenance commands
- a separate demo UI

and instead behave like one coherent application.

## Required Outcome

For interactive terminals, these entry points should land in the same app shell:

- `tele-cli`
- `tele-cli menu`
- `tele-cli setup`
- `tele-cli update`
- `tele-cli uninstall`

The shell should decide which flow to show based on current state and requested startup action.

## Why This Matters

The operator needs one dependable local place to:

- understand health
- finish setup
- repair conflicts
- update or reinstall
- uninstall safely

If those actions stay split across raw shell prompts and semi-separate flows, the product will remain harder to trust and maintain.

## Core Rule

The interactive app shell is the product's control plane for human local use.

Shell scripts and subcommands may bootstrap or provide non-interactive fallback behavior, but they should not replace the interactive shell as the main experience.

## Entry-Point Rules

### `tele-cli`

This should open the app shell home flow by default.

### `tele-cli menu`

This is an alias for opening the same app shell.

### `tele-cli setup`

In an interactive terminal, this should open the app shell in setup context and route the operator to the first incomplete setup step.

### `tele-cli update`

In an interactive terminal, this should open the app shell in update context and show progress and post-update state in the shell.

### `tele-cli uninstall`

In an interactive terminal, this should open the app shell in uninstall context and require explicit confirmation there.

### Non-interactive use

Without a TTY, the CLI may keep direct subcommand behavior for scripting and automation.

## Shell Responsibilities

The app shell should own these flows end to end:

- startup detection
- setup
- Telegram token save and validation
- Telegram pairing completion
- Codex login-required visibility
- duplicate service registration detection
- stale runtime lock detection
- update and repair actions
- uninstall confirmation
- handoff to local chat when selected

## Screen Model

The exact visuals can evolve, but the shell needs these stateful views.

### Splash/startup

Shown briefly during interactive startup while the shell gathers runtime state.

### Main status screen

The default home view after startup or after completing a flow.

### Setup flow screens

At minimum:

- dependency readiness
- Telegram token entry
- Telegram pairing

### Conflict and recovery screens

For cases such as:

- duplicate service registrations
- live conflicting app owner
- stale setup or runtime lock

### Update and repair screen

Shows what the app is doing during update or repair instead of dropping work to plain shell output.

### Uninstall confirmation screen

Provides a deliberate final checkpoint before destructive cleanup.

## Backend Contract

The shell should talk to a backend layer instead of embedding raw install or service-manager logic directly in rendering code.

That backend should expose:

- current status
- menu/action availability
- setup actions
- update actions
- uninstall actions
- dependency checks
- Telegram validation and pairing
- duplicate service registration analysis and repair

## Migration Boundaries

### What should move into the shell

- operator-facing prompts
- progress reporting for interactive flows
- recovery decisions
- final summaries

### What can stay outside the shell

- minimal bootstrap required to launch `tele-cli`
- non-interactive script entry points
- low-level service manager integration
- low-level dependency installation logic

## Done Criteria

The migration is complete when:

- interactive setup, update, and uninstall all run through the shell
- the shell can represent real runtime state, not demo-only state
- operator-facing conflict resolution happens in the shell
- direct print-and-exit behavior is no longer the primary interactive path
