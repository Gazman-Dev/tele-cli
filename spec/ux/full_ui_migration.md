# Full UI Migration Spec

## 1. Document Status

- Status: draft
- Date: March 15, 2026
- Related specs:
  - [spec/ux_spec.md](/C:/git/MiniC/spec/ux_spec.md)
  - [spec/codex_app_server_integration_spec.md](/C:/git/MiniC/spec/codex_app_server_integration_spec.md)
  - [spec/services_and_lifecycle_spec.md](/C:/git/MiniC/spec/services_and_lifecycle_spec.md)
- Purpose: define how Tele Cli moves from the current mixed CLI/menu/demo experience to one unified interactive app flow based on the demo UI

## 2. Goal

Tele Cli should have one user-facing control surface.

That control surface is the full-screen app flow currently represented by the demo UI.

Required outcome:

- install launches the Tele Cli app flow
- update launches the same Tele Cli app flow
- reinstall launches the same Tele Cli app flow
- direct `tele-cli` launches the same Tele Cli app flow
- the old plain-text menu/setup/update flows stop being primary UX surfaces

The terminal app becomes the single place where the user:

- sees startup and status
- completes setup
- resolves reinstall/update situations
- manages lifecycle actions
- uninstalls

## 3. Core Product Rule

The app flow is the only control point of the product.

Implications:

- install scripts should do the minimum required to get the Tele Cli app running
- setup should not ask raw terminal questions once the app shell is available
- update should not print a short message and exit when launched interactively
- reinstall should not short-circuit to "already installed" and exit
- lifecycle conflicts should resolve inside the app flow, not by dropping the user back to plain terminal prompts

## 4. Current Problem

Today the product is split across three UX layers:

- plain setup/update/install logic in [src/setup/setup_flow.py](/C:/git/MiniC/src/setup/setup_flow.py) and [src/setup/admin.py](/C:/git/MiniC/src/setup/admin.py)
- a basic text menu in [src/tui/menu.py](/C:/git/MiniC/src/tui/menu.py)
- a richer demo shell in [src/demo_ui](/C:/git/MiniC/src/demo_ui)

This causes inconsistent behavior:

- update or reinstall can complete some lifecycle work and exit too early
- setup still uses plain prompts for token entry and pairing
- the richer splash/status/menu flow is not yet the real product shell

## 5. Product Direction

Promote the demo UI into the real app shell.

Do not treat the demo as a mock anymore.

Instead:

- keep its visual language and flow structure
- replace fake/demo state transitions with real setup/runtime/install state
- make it the default interactive entry point

## 6. Entry Point Rules

### 6.1 Remote install

Remote install should do only bootstrap work:

1. download or install Tele Cli package
2. install enough dependencies to launch Tele Cli itself
3. register the `tele-cli` command
4. invoke the Tele Cli app flow

It should not try to complete full setup in the shell before the app is shown.

### 6.2 Local install or reinstall

If the user runs an installer or setup entry on a machine that already has Tele Cli:

- perform minimal bootstrap/update checks
- then launch the app flow
- inside the app flow, show the actual state and available actions

### 6.3 `tele-cli`

`tele-cli` with no subcommand should launch the full-screen app shell directly.

That shell should always start with:

- splash screen
- state detection
- status screen and menu

### 6.4 `tele-cli setup`

If invoked interactively, this should enter the app shell in setup context.

Expected behavior:

- launch the same app shell
- route the user into the required setup step if setup is incomplete
- return to the main status/menu view after setup completes

### 6.5 `tele-cli update`

If invoked interactively, this should enter the app shell in update context.

Expected behavior:

- launch the same app shell
- show update/reinstall progress in the app shell
- return to the main status/menu view when finished

### 6.6 Non-interactive contexts

If no TTY is available:

- keep a narrow non-interactive path for scripting and automation
- print concise machine-usable status
- do not attempt to launch the full-screen app shell

The full UI migration applies to human interactive use, not headless automation.

## 7. Minimal Installer Principle

Installer must do the minimum steps required to make the app runnable.

That means:

- bootstrap package/runtime dependencies required to execute `tele-cli`
- do not force the entire setup or service registration sequence before the app starts
- defer operator decisions and visible progress into the app flow

Examples:

- if npm or Codex is missing, installer may bootstrap only what is required for the app to start, then let the app show dependency installation progress
- if Telegram token is missing, installer must not prompt in raw shell; the app should own the token screen
- if the service is already installed, installer should not exit with a one-line message; the app should own the update/restart/reinstall resolution

## 8. Required App Flow States

The app shell must be able to render these real operational modes:

- first install
- incomplete setup
- healthy installed state
- update in progress
- reinstall over existing install
- duplicate service registration detected
- stale lock detected
- service restart in progress
- Codex auth required
- Telegram not paired
- degraded runtime
- uninstall confirmation

## 9. Screen Model

The real app shell should preserve the existing demo screen model, but wire it to live behavior.

### 9.1 Splash screen

Shown on interactive startup for:

- install
- update
- reinstall
- normal app launch

Purpose:

- establish a consistent product identity
- hide noisy bootstrap details
- transition into real status detection

### 9.2 Main status screen

This becomes the home screen after startup and after any completed action.

It must show live values for:

- service state
- Codex state
- Telegram state
- current status line

### 9.3 Setup screens

These replace raw prompt-based setup.

Required first real screens:

- Telegram bot token screen
- Telegram pairing screen

Later screens may include:

- Codex account auth status
- dependency repair

### 9.4 Update/reinstall screen

Update should become a first-class in-app flow, not a brief print statement.

It must show:

- what was detected
- what will be updated/repaired
- progress while the update runs
- return to main status on completion

### 9.5 Conflict-resolution screens

The app should own user-facing decisions for:

- duplicate service registration
- stale lock
- live owner conflict
- reinstall over existing install

These should be rendered as app screens, not raw `ask_choice(...)` prompts, when interactive UI is active.

## 10. Real Behavior To Replace Demo Behavior

The following demo-only behaviors must be replaced with real logic:

- fake token validation
- fake pairing trigger and fake pairing code generation flow
- fake update spinner steps
- fake service restart step
- fake uninstall flow
- fake status state stored only in `DemoState`

The final app shell should read and act on:

- real setup state
- real auth state
- real runtime state
- real service-manager state
- real lock inspection state

## 11. UX Contract For Install, Update, Reinstall

### 11.1 Install

Expected sequence:

1. minimal bootstrap
2. app shell launches
3. splash
4. setup flow if required
5. main status/menu

### 11.2 Update

Expected sequence:

1. minimal bootstrap to ensure app can launch
2. app shell launches in update context
3. splash
4. update/restart flow shown in UI
5. main status/menu

### 11.3 Reinstall

Expected sequence:

1. detect existing installation
2. app shell launches
3. splash
4. user sees installed state and available actions
5. if update/repair/restart is needed, the user performs it from the UI
6. main status/menu remains the landing point

## 12. Ownership Of Decisions

The app shell should own these user-visible decisions:

- continue setup
- reuse existing Telegram token
- pair new Telegram chat
- update now
- repair duplicate registrations
- restart service
- uninstall

The installer should own only:

- bare minimum bootstrap required to start the shell

## 13. Architecture Implications

This migration implies three architectural changes.

### 13.1 Demo UI becomes real UI

The code under [src/demo_ui](/C:/git/MiniC/src/demo_ui) should be promoted into production UI code, not kept as a side demo.

### 13.2 Plain prompt flows become services/backend logic

Code in:

- [src/setup/setup_flow.py](/C:/git/MiniC/src/setup/setup_flow.py)
- [src/setup/admin.py](/C:/git/MiniC/src/setup/admin.py)
- [src/setup/recovery.py](/C:/git/MiniC/src/setup/recovery.py)
- [src/tui/menu.py](/C:/git/MiniC/src/tui/menu.py)

should stop owning the interactive UX directly.

Instead, they should expose operations and state transitions that the app shell can call.

### 13.3 Shared app state model is needed

`DemoState` is not sufficient for production.

We will need a real app-shell view model that combines:

- setup state
- install/update context
- service-manager state
- runtime state
- auth state
- lock/conflict state
- transient UI state like current screen, progress, selection, and errors

## 14. Execution Plan

### Phase A: define the production app shell

- declare the real shell entry module
- decide whether `src/demo_ui` is renamed or absorbed into `src/tui`
- define the real app-shell state model
- map current demo screens to production responsibilities

### Phase B: route interactive entry points into the shell

- `tele-cli`
- `tele-cli setup`
- `tele-cli update`
- reinstall/install completion path

All interactive entry points should land in the same shell.

### Phase C: replace setup prompts with UI screens

- token entry uses real validation
- pairing screen uses real Telegram polling/pairing
- setup completion screen triggers real service registration/start

### Phase D: replace update/reinstall shell prints with UI flows

- update progress
- duplicate registration repair confirmation
- reinstall existing install resolution
- restart/health verification results

### Phase E: replace menu-backed status and lifecycle actions

- main status screen reads live state
- restart action runs real lifecycle work
- uninstall uses real confirmation and removal logic

### Phase F: remove legacy interactive code paths

- old plain text menu no longer primary
- raw `ask_text` and `ask_choice` paths remain only for non-interactive fallback or truly minimal bootstrap cases

## 15. Acceptance Criteria

This migration is complete only when:

- first install launches the full-screen Tele Cli app shell
- update launches the same app shell instead of printing and exiting
- reinstall launches the same app shell instead of printing "already installed" and exiting
- setup is completed entirely through the app shell
- the status/menu screen is the landing point after setup and update
- lifecycle conflict resolution is shown inside the app shell for interactive runs
- non-interactive install/update paths still work without the shell

## 16. Non-Goals

This document does not require:

- changing Telegram as the operator surface
- changing the runtime/service architecture
- web UI support
- replacing non-interactive automation paths

It is specifically about making the demo-style terminal app the real interactive product shell.

## 17. Recommended Next Implementation Slice

The best first implementation slice is:

1. promote the demo shell into a real app-shell module
2. make `tele-cli`, `tele-cli setup`, and `tele-cli update` all enter that shell when interactive
3. replace the demo setup screens with real token/pairing flows
4. keep the current backend install/setup/update logic behind callable operations used by the shell

That gives one consistent interactive control point quickly, while minimizing churn in the lifecycle backend.
