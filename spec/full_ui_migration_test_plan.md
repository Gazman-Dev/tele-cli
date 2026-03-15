# Full UI Migration Test Plan

## 1. Document Status

- Status: draft
- Date: March 15, 2026
- Parent spec: [spec/full_ui_migration_spec.md](/C:/git/MiniC/spec/full_ui_migration_spec.md)
- Related specs:
  - [spec/ux_spec.md](/C:/git/MiniC/spec/ux_spec.md)
  - [spec/services_and_lifecycle_spec.md](/C:/git/MiniC/spec/services_and_lifecycle_spec.md)
  - [spec/codex_app_server_integration_test_plan.md](/C:/git/MiniC/spec/codex_app_server_integration_test_plan.md)
- Purpose: define the automated and smoke-test coverage required to migrate Tele Cli’s real interactive UX to the demo-style app shell

## 2. Goal

The UI migration is complete only when the demo-style app shell is the single interactive control point for:

- install
- update
- reinstall
- normal launch
- setup
- lifecycle repair prompts

This test plan defines how to verify that behavior.

## 3. Testing Strategy

We should test the UI migration at four layers.

### 3.1 View-model and flow unit tests

Focus on deterministic screen-state decisions:

- which screen opens first
- what menu items appear
- what context is active
- which backend action a UI decision triggers

### 3.2 Shell integration tests

Run the real app shell against fakes:

- fake installer backend
- fake service manager
- fake Telegram client
- fake lock/ownership inspector
- fake Codex/app-server runtime state

These tests should assert screen transitions and invoked backend actions.

### 3.3 CLI entry tests

Verify that:

- `tele-cli`
- `tele-cli setup`
- `tele-cli update`
- install completion path
- reinstall path

all land in the same app shell in interactive mode.

### 3.4 Narrow interactive smoke tests

Small manual or pseudo-interactive smoke checks for:

- startup splash appears
- setup screens render
- update screen renders
- status/menu returns after actions

These should stay narrow and target user-visible continuity.

## 4. Harnesses Needed

### 4.1 Fake app-shell backend

A backend facade for the UI to call instead of raw setup/update code directly.

It should simulate:

- dependency installation
- Telegram token validation
- Telegram pairing
- service registration/start/restart
- update/reinstall operations
- uninstall

### 4.2 Fake shell input/output driver

Requirements:

- sends key presses
- sends text entry
- records rendered screens or rendered state snapshots
- supports deterministic transitions without real terminal timing dependencies

### 4.3 Fake install launcher

Requirements:

- simulates first install, update install, and reinstall entry
- records whether install bootstrap stops after minimal setup
- records whether the app shell launches

### 4.4 Fake lifecycle state sources

Requirements:

- fake setup state
- fake runtime state
- fake auth state
- fake lock/service conflict states
- fake service-manager registration analysis

These allow the app shell to render real states without touching the real machine.

## 5. Primary Test Areas

### 5.1 Unified interactive entry

We need explicit tests proving that all interactive entry points converge into one shell.

### 5.2 Setup migration

We need explicit tests proving setup is no longer prompt-driven in normal interactive use.

### 5.3 Update and reinstall migration

We need explicit tests proving update/reinstall no longer short-circuit to print-and-exit behavior.

### 5.4 Conflict-resolution migration

We need explicit tests proving duplicate registration, stale lock, and reinstall conflicts are shown inside the shell.

### 5.5 Status/menu continuity

We need explicit tests proving the app always returns to the main status/menu screen after completing interactive actions.

## 6. Detailed Test Scenarios

### 6.1 First install launch

- minimal installer bootstrap completes
- installer launches the app shell immediately
- splash screen appears first
- app detects incomplete setup
- user is routed into token setup screen
- after token setup, user is routed into pairing screen
- after pairing completes, setup completion screen appears
- app returns to main status/menu screen

### 6.2 First install with dependencies missing

- installer performs only the minimum bootstrap required to launch Tele Cli
- app shell owns visible dependency installation progress
- user does not see raw shell prompts for dependency decisions
- after dependency completion, setup continues in the shell

### 6.3 Normal launch after setup

- `tele-cli` launches splash
- app loads live status
- app lands on main status/menu screen
- no setup screens are shown

### 6.4 Interactive `tele-cli setup`

- command enters the same app shell
- setup context is selected
- user is routed to the first incomplete setup step
- on completion, app returns to main status/menu screen
- app does not exit immediately after setup

### 6.5 Interactive `tele-cli update`

- command enters the same app shell
- update context is selected
- update progress screen is shown
- managed update backend is invoked
- after update completes, app returns to main status/menu screen
- app does not print "update complete" and exit as the primary UX

### 6.6 Reinstall over existing install

- reinstall bootstrap detects existing install
- app shell launches instead of exiting early
- splash is shown
- main status/menu is shown with existing state
- user can choose update/restart/repair behavior from the shell

### 6.7 Existing install with already-configured Telegram

- app shell launches
- token screen is skipped
- pairing screen is skipped
- status/menu is shown directly

### 6.8 Existing install with saved token but unpaired Telegram

- app shell launches
- token screen is skipped or shown as prefilled/reuse state
- pairing screen is shown
- successful pairing returns to main status/menu screen

### 6.9 Existing install with Codex auth required

- app shell launches
- main status shows Codex auth required state
- app remains usable as the control point
- user is not dropped to raw shell output

### 6.10 Duplicate service registration detected during setup

- setup backend detects duplicate registration
- app shell shows conflict-resolution screen
- both registrations are listed
- user can confirm removal of the duplicate
- setup then continues
- if declined, app remains in a clear blocked/error state inside the shell

### 6.11 Duplicate service registration detected during update

- update backend detects duplicate registration
- app shell shows repair/update decision
- if user accepts repair, duplicate is removed and update continues
- if user declines, update is cancelled but app stays in the shell

### 6.12 Stale lock detected on interactive launch

- app shell launches
- stale-lock situation is shown in the UI
- user can heal, ignore, or cancel inside the shell
- after resolution, app returns to normal status/menu or setup flow

### 6.13 Live owner conflict on interactive launch

- app shell launches
- app shows that another instance is active
- user can choose keep other, stop other, or exit
- no raw `ask_choice(...)` prompt is shown in interactive UI mode

### 6.14 Service restart action from menu

- user chooses restart service
- restart progress screen appears
- service-manager backend restart action is invoked
- status line updates after restart
- app returns to main status/menu screen

### 6.15 Uninstall action from menu

- uninstall confirmation screen appears
- exact confirmation text is required
- on cancel, app returns to main status/menu screen
- on confirm, uninstall backend is invoked
- app exits only after completion

### 6.16 Update from menu

- user chooses update in the app shell
- same update screen is shown as `tele-cli update`
- same managed update backend is invoked
- app returns to main status/menu screen

### 6.17 Setup interruption and resume

- app shell launches after interrupted setup
- shell shows incomplete setup state
- user is routed to the correct next setup step
- setup does not restart from the beginning unless explicitly chosen

### 6.18 Reinstall after interrupted setup

- reinstall launches shell
- shell shows interrupted setup state
- user can resume or restart setup from inside the shell

### 6.19 Non-interactive fallback

- non-interactive `tele-cli setup` does not attempt to launch full-screen shell
- non-interactive `tele-cli update` does not attempt to launch full-screen shell
- scripted fallback remains concise and functional

## 7. Screen-Specific Assertions

### 7.1 Splash screen

- appears on install, update, reinstall, and normal launch in interactive mode
- uses the real product shell, not a separate demo binary
- transitions into the correct next screen

### 7.2 Main status/menu screen

- shows real service state
- shows real Codex state
- shows real Telegram state
- shows real one-line status message
- reflects live setup/runtime/install conditions

### 7.3 Token setup screen

- uses real token validation backend
- validation failure keeps user in the screen with a clear error
- success moves to pairing or completion, not to raw shell

### 7.4 Pairing screen

- uses real Telegram pairing backend
- pending state is visible
- success moves to completion/status
- failure remains in-shell with a clear error

### 7.5 Update screen

- shows update progress in shell
- shows repair or blocked state in shell on duplicate conflict
- shows success/failure inside shell

### 7.6 Conflict screens

- list detected registrations or lock owners
- identify the recommended action
- keep the user inside the shell during resolution

## 8. Backend Contract Tests Needed

The app shell will require a clean backend interface. We should test that interface directly.

Required backend operations:

- detect install state
- detect setup completeness
- validate Telegram token
- begin or continue Telegram pairing
- detect service conflicts
- repair duplicate registrations
- perform managed update
- restart service
- uninstall

Contract tests:

- each backend operation returns structured state, not raw print text
- each backend operation exposes user-presentable error codes/messages
- long-running operations expose progress steps the shell can render

## 9. Migration Safety Tests

We should explicitly guard against regressions during the UI migration.

- interactive setup does not call raw `ask_text(...)` for token entry
- interactive update does not use print-and-exit as the primary user path
- interactive duplicate repair does not use raw `ask_choice(...)`
- the old text menu is no longer the primary interactive shell after migration completes
- install bootstrap does not overrun into full setup before the shell launches

## 10. Suggested Test File Layout

Suggested additions:

- `tests/test_app_shell_entry.py`
- `tests/test_app_shell_setup_flow.py`
- `tests/test_app_shell_update_flow.py`
- `tests/test_app_shell_reinstall_flow.py`
- `tests/test_app_shell_conflicts.py`
- `tests/test_app_shell_status.py`
- `tests/test_app_shell_uninstall.py`
- `tests/fakes/fake_app_shell_backend.py`
- `tests/fakes/fake_terminal_driver.py`

Likely existing files to update:

- `tests/test_cli.py`
- `tests/test_admin.py`
- `tests/test_installer_lifecycle.py`
- `tests/test_host_service.py`

## 11. Exit Criteria

The full UI migration should not be considered complete until all of the following are true:

- install launches the unified app shell
- update launches the unified app shell
- reinstall launches the unified app shell
- setup is completed through the shell
- update/reinstall conflicts are resolved through the shell
- the main status/menu screen is the stable landing point after setup and update
- interactive raw prompt flows are no longer the primary UX path
- non-interactive fallback paths still work

## 12. Recommended First Tests To Write

1. `tele-cli`, `tele-cli setup`, and `tele-cli update` all enter the same app shell in interactive mode.
2. first-install flow launches splash, token screen, pairing screen, then main status/menu.
3. interactive update shows update screen and returns to main status/menu.
4. reinstall over existing install launches shell instead of exiting early.
5. duplicate registration during update is surfaced in-shell and repair confirmation stays in-shell.
6. stale lock during startup is surfaced in-shell and resolved without raw prompts.
7. setup resume after interrupted setup lands on the correct next screen.
8. uninstall confirmation stays in-shell and returns to status/menu on cancel.
