# Services And Lifecycle Spec

## 1. Document Status

- Status: draft
- Date: March 14, 2026
- Related specs:
  - [spec/codex_app_server_integration_spec.md](/C:/git/MiniC/spec/codex_app_server_integration_spec.md)
  - [spec/codex_app_server_integration_test_plan.md](/C:/git/MiniC/spec/codex_app_server_integration_test_plan.md)
- Purpose: define the operational lifecycle contract for Tele Cli so that Telegram remains reachable and Codex remains recoverable across installs, updates, crashes, and host restarts

## 2. Core Requirement

At the end of the day, the system must restore and maintain these two capabilities:

- Tele Cli is always listening for Telegram updates when the host is healthy and the service is enabled.
- Tele Cli can always restore Codex availability, either by reconnecting to a healthy app-server child or by restarting it automatically.

If one of those capabilities is temporarily unavailable, the runtime must move to an explicit degraded state, surface that state clearly, and keep trying to recover unless operator action is required.

## 3. Operational Invariants

The service design should preserve these invariants.

### 3.1 Single service owner per state directory

For one state directory, there must be exactly one active Tele Cli service owner at a time.

Implications:

- no split-brain service instances writing the same state files
- no two app-server supervisors competing for the same sessions
- no duplicate Telegram pollers using the same recovery state

### 3.2 One Codex App Server supervisor per service owner

Each active Tele Cli service instance owns at most one Codex App Server child for that state directory.

Implications:

- if the child dies, the same owner restarts it
- if the owner dies, the replacement owner becomes responsible for restart and recovery

### 3.3 Telegram listener is a first-class subsystem

Telegram listening must not depend on an active Codex turn.

Implications:

- Telegram polling starts even if Codex auth is missing
- Telegram polling stays up during Codex restarts
- Telegram can deliver login links, degraded-state warnings, and recovery prompts even while Codex is unavailable

### 3.4 Session mapping is durable

The session-to-thread mapping must survive service restarts and app upgrades.

Implications:

- state files persist across process restarts
- restarting the service must not silently create replacement threads for existing sessions
- failed resume must be explicit and recoverable

### 3.5 Recovery is explicit, not silent

When automatic recovery cannot preserve continuity safely, Tele Cli must stop short of destructive repair and mark the relevant service or session as degraded.

Examples:

- failed `thread/resume`
- stale approval after restart
- conflicting live service with unknown ownership
- incompatible app-server protocol version

## 4. Runtime Components

The operational lifecycle should treat the system as five cooperating components.

### 4.1 Installer

Responsible for:

- install
- update
- uninstall
- service registration with the host service manager

### 4.2 Host-managed Tele Cli service

Responsible for:

- boot on login or system startup, depending on platform policy
- restarting the Tele Cli process after crashes
- exposing start, stop, restart, and status actions

### 4.3 Tele Cli process

Responsible for:

- loading state
- acquiring service ownership
- polling Telegram
- supervising Codex App Server
- routing sessions and approvals

### 4.4 Codex App Server child

Responsible for:

- hosting durable Codex threads
- handling turns, approvals, and account state

### 4.5 State directory

Responsible for:

- preserving continuity across restarts, crashes, and updates

Expected files after migration:

- `auth.json`
- `config.json`
- `runtime.json`
- `sessions.json`
- `approvals.json`
- `codex_server.json`
- `app.lock`
- `recovery.log`

## 5. Lifecycle States

### 5.1 Service states

Recommended service-level states:

- `STARTING`
- `RUNNING`
- `DEGRADED`
- `RESTARTING`
- `STOPPING`
- `STOPPED`
- `FAILED`

### 5.2 Telegram subsystem states

- `STARTING`
- `RUNNING`
- `BACKOFF`
- `DEGRADED`
- `STOPPED`

### 5.3 Codex subsystem states

- `STOPPED`
- `STARTING`
- `INITIALIZING`
- `RUNNING`
- `AUTH_REQUIRED`
- `BACKOFF`
- `DEGRADED`
- `STOPPING`

### 5.4 Session states

- `ACTIVE`
- `IDLE`
- `RUNNING_TURN`
- `WAITING_APPROVAL`
- `INTERRUPTED`
- `RECOVERING_TURN`
- `DETACHED`
- `DEGRADED`

## 6. Startup Lifecycle

On host or service-manager start:

1. Tele Cli process starts.
2. It loads config and durable state files.
3. It acquires the `app.lock` or resolves a conflict.
4. It marks service state `STARTING`.
5. It starts Telegram polling immediately.
6. It launches Codex App Server.
7. It completes `initialize` and account-state checks.
8. It marks Codex `RUNNING` or `AUTH_REQUIRED`.
9. It marks service `RUNNING` if Telegram is healthy and Codex is either `RUNNING` or explicitly waiting on auth.

Important rule:

- Telegram startup must not wait for Codex startup to complete.

## 7. Normal Steady State

Healthy steady state means:

- service owner lock is valid
- Telegram polling is active
- Codex App Server is healthy or recoverable
- session mappings are loaded
- approvals can be received and answered

Normal behavior:

- incoming Telegram messages are accepted
- `/status` reports both Telegram and Codex subsystem health
- user messages route to the active session
- new Telegram-created Codex threads use dangerous-mode defaults unless config overrides them
  - `sandbox = "danger-full-access"`
  - `approvalPolicy = "never"`
- Codex turns can continue through short-lived network or app-server restarts when safe

## 8. Shutdown Lifecycle

Planned shutdown should follow this order:

1. mark service `STOPPING`
2. stop accepting new turn starts
3. keep Telegram alive long enough to report shutdown if needed
4. persist in-memory session and approval state
5. stop or detach from Codex App Server according to policy
6. release `app.lock`
7. mark service `STOPPED`

Rules:

- a graceful stop must not corrupt durable session mappings
- pending approvals must be written before exit
- active turns must not be marked completed unless a real completion event was observed

## 9. Device Restart And Host Reboot

Host reboot is a normal lifecycle event, not an error.

Required behavior after reboot:

- host service manager starts Tele Cli automatically
- Tele Cli reacquires ownership
- Telegram polling resumes without manual intervention
- Codex App Server is relaunched automatically
- stored sessions remain available
- stale approvals are surfaced as stale, not resumed blindly

If the reboot occurred during an active turn:

- the turn must be marked unknown or interrupted, not completed
- the session remains mapped to the original `thread_id`
- recovery should prefer `thread/resume` if the protocol supports safe continuation
- if continuation cannot be proven safe, the session becomes `DEGRADED`

## 10. App Update Lifecycle

An update should behave like a controlled restart, not a fresh install.

### 10.1 Update goals

- keep the same state directory
- keep the same service identity
- preserve Telegram auth
- preserve Tele Cli session mappings
- restart service cleanly onto the new code

### 10.2 Required update flow

Recommended flow:

1. installer detects existing installation
2. installer validates whether the service is already registered
3. installer stops the running service through the host service manager
4. installer updates package files
5. installer preserves state files untouched unless a migration is required
6. installer runs any state migration step
7. installer restarts the service
8. installer verifies Telegram listener and Codex supervisor are healthy

### 10.3 State migration rules

If app updates require state schema changes:

- migrations must be explicit and versioned
- migration must be idempotent
- migration failure must leave the previous durable files recoverable
- runtime must refuse partial or ambiguous migrated state

## 11. Duplicate Install And Reinstall Behavior

Running setup or install twice is expected and must not create duplicate background services.

### 11.1 Required invariant

There must be one installed service registration per intended state directory and operator environment.

### 11.2 Safe duplicate-install behavior

If the user runs install again:

- detect the existing installation
- detect the existing service registration
- offer update, repair, restart, or uninstall behavior
- do not register a second background service pointing at the same state directory

### 11.3 If the user wants to kill one copy

If two processes or services appear to exist, Tele Cli must distinguish these cases:

- same service, old process still shutting down
- one live service and one foreground debug process
- stale lock from a dead process
- genuinely duplicated service registrations

Required handling:

- if one process owns the active lock and is healthy, keep that one
- if one process is stale, clear the stale lock and stale registration
- if two live owners are detected for the same state directory, one must be stopped before proceeding
- the system must never allow both to continue polling Telegram and writing the same state

### 11.4 Installer repair mode

If duplicate service registrations are found, installer should:

- list both registrations
- identify which executable and state directory each uses
- recommend the canonical registration
- disable and remove the duplicate registration only after explicit confirmation

## 12. Service Conflict Resolution

Conflict resolution should be deterministic.

### 12.1 Live service conflict

If a new Tele Cli process starts and finds a live owner:

- if the user explicitly launched a foreground debug session, offer `attach-like status`, `stop other`, or `exit`
- if this is a background service start, background start should exit without taking ownership

### 12.2 Stale lock

If `app.lock` exists but the owner process is dead:

- classify it as stale
- inspect whether a child Codex process still exists
- if the orphaned child belongs to the same state directory, terminate or adopt according to policy
- clear the stale lock and continue startup

### 12.3 Unknown ownership

If the runtime cannot prove process ownership safely:

- do not kill automatically
- mark degraded
- require explicit operator confirmation

## 13. Codex App Server Crash Recovery

Codex App Server crash must not stop Telegram listening.

Required behavior:

- Telegram subsystem remains up
- service marks Codex `BACKOFF` or `DEGRADED`
- supervisor attempts restart with bounded backoff
- successful restart triggers re-initialize and account-state refresh
- stored sessions stay mapped to their original `thread_id`

If a crash happens during an active turn:

- do not emit a fake final answer
- notify the operator that execution was interrupted
- preserve enough turn metadata to support recovery messaging

If repeated restart attempts fail:

- Telegram must still answer `/status`
- service remains reachable for auth and repair actions
- runtime status clearly states that Codex is unavailable

## 14. Telegram Failure Recovery

Telegram connectivity failures are also recoverable events.

Required behavior:

- polling failures enter `BACKOFF`
- service does not exit on transient Telegram failures
- Codex may remain healthy while Telegram is reconnecting
- once Telegram recovers, the service continues with existing sessions

If Telegram is down but Codex is healthy:

- do not create new turns from nowhere
- active Codex work may continue if already started
- final delivery may need buffering until Telegram recovers or an operator-visible degraded state is recorded

## 15. Auth Lifecycle

Telegram auth and Codex account auth are separate and must remain separate operationally.

### 15.1 Telegram auth lifecycle

- configured
- pending pairing
- paired
- reset

Resetting Telegram auth should:

- revoke operator control for incoming chat messages
- preserve session history and Codex metadata unless user explicitly wipes all state

### 15.2 Codex auth lifecycle

- unknown
- ready
- login required
- login pending
- expired

When Codex auth expires:

- Telegram remains up
- `/status` shows auth required
- Tele Cli can send login URL or recovery instructions to Telegram
- session mappings are preserved

## 16. Manual Process Kills

Users will kill processes. The design should assume that.

### 16.1 User kills Tele Cli process

Expected outcome:

- host service manager restarts Tele Cli
- lock ownership is re-established
- Telegram polling resumes
- Codex App Server is restarted if needed

### 16.2 User kills Codex App Server only

Expected outcome:

- Tele Cli detects child exit
- Telegram remains active
- Tele Cli restarts Codex automatically

### 16.3 User kills the "wrong" process during duplicate-conflict cleanup

Expected outcome:

- surviving service either keeps or reacquires ownership
- stale state is cleaned up on next startup
- no duplicate Telegram pollers remain active

## 17. Foreground Debug Mode Versus Background Service

Foreground debug must not create a second long-lived owner accidentally.

Rules:

- debug mode should detect the active background owner before starting
- debug mode may temporarily take over only through explicit conflict resolution
- when debug mode exits, background service should be restartable cleanly

Recommended policy:

- background service is canonical for always-on operation
- foreground debug is temporary and operator initiated

## 18. State Durability Rules

The following writes must be treated as high-value durability points:

- session creation and thread-id persistence
- approval request persistence
- service state transitions affecting recovery
- app-server protocol/account state snapshots

Rules:

- write before acknowledging critical transitions when possible
- partial writes must not leave unreadable JSON
- on write failure, mark degraded and log clearly

## 19. Required Operator Commands

Lifecycle visibility requires explicit commands.

Minimum commands:

- `/status`
- `/new`
- `/sessions`
- `/stop`

Recommended local/admin commands:

- `tele-cli service`
- `tele-cli debug`
- `tele-cli reset-auth`
- `tele-cli repair`
- `tele-cli uninstall`

`/status` should report:

- service state
- Telegram state
- Codex state
- auth readiness
- active session id
- pending approval count
- degraded conditions needing action

## 20. Edge Cases Checklist

The implementation must explicitly handle all of these.

- host reboot while service idle
- host reboot during active turn
- app update while service idle
- app update during active turn
- installer launched twice in quick succession
- service registered twice accidentally
- stale `app.lock` with no live process
- stale `app.lock` with orphaned Codex child
- live Tele Cli service plus manual debug run
- Tele Cli crash loop caused by bad config
- Codex App Server crash loop
- Telegram network outage
- Codex auth expires while Telegram remains healthy
- Telegram auth reset while sessions still exist
- duplicate Telegram update delivery after reconnect
- restart with pending approval
- restart with interrupted turn
- failed state migration during update
- user kills Tele Cli
- user kills Codex child
- user kills the process they thought was duplicate but was the active owner

## 21. Recommended Host Integration

For always-on behavior, Tele Cli should run under the host's native service manager rather than relying on an interactive shell.

Required properties:

- automatic start on host boot or user login
- automatic restart on crash
- standard stop and restart commands
- one canonical registration per install

This is necessary to satisfy the requirement that Tele Cli always listens for Telegram and can restore Codex after failures.

## 22. Acceptance Criteria

This lifecycle design is acceptable only if all of the following are true:

- a normal reboot restores Telegram listening automatically
- a Codex child crash does not require manual restart
- a duplicate install does not create duplicate active services
- a stale lock does not block recovery permanently
- session-to-thread mapping survives update and restart
- Telegram remains available for status and auth flows while Codex is degraded
- conflicting live owners are surfaced and resolved explicitly
- the system always converges back to one Telegram listener and one Codex supervisor per state directory

## 23. Implementation Implications

This document implies the following engineering work:

- service-manager-aware install and update flow
- stronger ownership metadata in `app.lock`
- explicit degraded and backoff states in runtime models
- restart supervisor for Codex App Server
- repair tooling for duplicate registrations and stale state
- tests for reboot, restart, and duplicate-owner scenarios

These should be added to the implementation backlog and test plan before the main app-server migration proceeds.

## 24. Required Test Translation

This lifecycle spec is not complete until its operational guarantees are converted into automated tests.

### 24.1 Testing priority

Recommended order:

1. adapter-level tests with fake service manager, fake ownership probes, fake Telegram, and fake app server
2. service-level integration tests that combine lifecycle recovery with session routing
3. narrow real-platform smoke tests for install/start/stop/restart only

This is the best tradeoff for now because it validates the lifecycle contract without making the test suite depend too early on OS-specific service tooling.

### 24.2 Minimum lifecycle test set

The minimum acceptable automated coverage should include:

- one-owner-per-state-directory enforcement
- stale lock recovery
- orphaned Codex child recovery
- duplicate service-registration detection and repair
- reboot auto-start recovery
- Tele Cli crash restart recovery
- Codex child crash restart recovery
- Telegram-up while Codex-down degraded operation
- update preserving state and service identity
- migration failure preserving recoverable prior state

### 24.3 Release gate

The app-server migration should not be considered release-ready until the lifecycle tests above pass alongside the protocol, session, turn, and approval tests.
