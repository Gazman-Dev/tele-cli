## Purpose

Build a local terminal application for **one operator** that:

* installs and runs **Codex**
* connects a **single Telegram bot** to a **single authorized Telegram chat**
* mirrors Codex terminal state to Telegram
* records a replayable terminal log
* exposes a debug mirror locally in the terminal/CLI
* protects against:

    * duplicate app instances
    * stale lockfiles
    * half-finished setup runs
    * orphaned Codex child processes

---

# Simplified V1 Model

## User model

Exactly one user is supported.

Store:

* one Telegram user ID
* one Telegram chat ID
* one bot token
* one active Codex session
* one local operator approval authority

No multi-user roles!

---

# OS Support

## Supported

* Linux
* macOS

## Not in V1

* Windows

---

# Setup Flow Changes

## First-run setup

The terminal setup wizard must do this in order:

1. Check whether another app instance is already running
2. Check whether a previous setup is already in progress or was interrupted
3. Ask the operator how to resolve conflicts if found
4. Detect whether `codex` is installed
5. Detect whether `npm` is installed
6. If `npm` is missing, install it
7. If Codex is missing, install Codex
8. Ask for Telegram bot token
9. Validate Telegram connectivity
10. Save config
11. Enter service mode

---

# npm and Codex Installation Requirements

## npm detection

The app must check whether `npm` is available in `PATH`.

## If npm is missing

The app must try to install it in an OS-appropriate way.

### Linux

Support common package managers with detection:

* `apt`
* `dnf`
* `yum`
* `pacman`
* `zypper`

### macOS

Use:

* Homebrew if available
* otherwise guide or automate installation of Homebrew only if explicitly enabled in config/design

## Important implementation rule

V1 should use an **installer strategy abstraction**:

* `detect_package_manager()`
* `install_npm()`
* `install_codex()`

This is cleaner than hardcoding shell commands all over the code.

## Codex install

After npm exists, install Codex.

---

# Single-Instance Protection

## Requirement

The app must protect against multiple copies of:

* the main service
* the setup wizard
* the Codex child runtime for this app instance

## Main mechanism

Use a **lock + PID metadata file** in the app state directory.

Example stored metadata:

* PID
* hostname
* username
* process start time if available
* mode: `setup` or `service`
* timestamp
* app version
* child Codex PID if active

## On startup

When the app starts, it must check:

1. Is there a lockfile?
2. Does the PID still exist?
3. Does the process appear to be this same app?
4. Is it in setup mode or service mode?
5. Is there an active Codex child attached to it?

## If a live instance exists

The app must not blindly continue.

It must prompt the operator:

* `kill` — terminate the conflicting app/Codex process if safely identifiable
* `ignore` — continue without taking ownership
* optionally `exit` — safest choice, recommended to include

You asked for kill/ignore; I strongly recommend also supporting `exit`.

---

# Setup Re-entrancy Protection

## Requirement

The setup wizard must not run twice at the same time.

## Setup lock

Create a dedicated **setup lock/state file**.

It must contain:

* status: `started`, `completed`, `failed`, `abandoned`
* PID
* timestamp
* partial progress markers:

    * npm installed?
    * codex installed?
    * telegram token saved?
    * telegram validated?

## On setup startup

If an existing setup state is found:

### Case 1 — setup is actively running

Prompt:

* `kill`
* `ignore`
* `exit`

### Case 2 — setup was interrupted/stale

Prompt:

* `resume`
* `restart`
* `ignore`
* `exit`

Even though you requested kill/ignore, for interrupted setup `resume` and `restart` are much better UX, so they should be added.

---

# Self-Heal Rules

## General principle

Whenever duplicate state or stale runtime state is detected, the app should not guess silently.

It should:

1. inspect
2. explain what it found
3. ask the operator what to do
4. execute the selected recovery action
5. log that choice

## Recovery scenarios

### Scenario A — stale main lock, process dead

Show:

* stored PID
* timestamp
* conclusion that process is no longer alive

Offer:

* `heal` or `resume` by clearing stale lock
* `ignore`
* `exit`

### Scenario B — live main app instance detected

Show:

* PID
* mode
* age
* whether Codex child exists

Offer:

* `kill`
* `ignore`
* `exit`

### Scenario C — stale setup run detected

Show:

* partial setup progress
* what appears complete and incomplete

Offer:

* `resume`
* `restart`
* `ignore`
* `exit`

### Scenario D — orphaned Codex process detected

The app should detect Codex processes likely started by this app but whose parent app is gone.

Offer:

* `kill`
* `adopt` if implementation supports taking ownership safely
* `ignore`

For V1, `kill` and `ignore` are enough. `adopt` is optional.

---

# Process Ownership Rules

## Requirement

Never kill arbitrary unrelated processes just because they contain the word `codex`.

The app must only auto-target processes that can be strongly linked to this app by one or more of:

* stored PID metadata
* same working directory marker
* same session metadata
* same wrapper command signature
* same state directory ownership

If ownership is uncertain:

* do not auto-kill
* warn the operator
* default to ignore/exit

---

# Revised Runtime Locking Model

Use these files in the app state directory:

* `app.lock` — single service instance lock
* `setup.lock` — setup instance lock
* `runtime.json` — current runtime metadata
* `auth.json` — single authorized Telegram chat/user
* `config.json` or equivalent
* `recovery.log` — recovery decisions and events

---

# Single User Authentication Update

## Pairing model

Only one Telegram chat can ever become the controlling chat in V1.

### Before pairing

Any incoming Telegram message gets a 5-digit code.

### Once paired

If a different chat or user sends a message:

* deny control
* reply that this bot is already paired to another chat
* do not generate a new pairing code unless the operator explicitly resets pairing locally

## Reset pairing

A local terminal command should exist:

* `reset-auth`

This clears the saved Telegram user/chat authorization and allows pairing again.

---

# Runtime Conflict Protection

## Requirement

The service must detect these conflicts at runtime too:

* Telegram polling loop started twice
* Codex child started twice
* recorder started twice
* debug server started twice

## Rule

Each subsystem should have an internal state machine and reject duplicate startup.

Example:

* if Codex session state is not `STOPPED`, do not start a second one
* if Telegram poller is already active, refuse second poller start

---

# Revised Session Constraints

## V1 session model

* exactly one Codex session
* exactly one Telegram controller chat
* exactly one debug mirror
* exactly one recorder

No parallel sessions.

---

# New Acceptance Criteria

V1 is complete only if all of the following are true:

1. App runs on Linux and macOS.
2. App detects missing npm.
3. App can install npm using OS/package-manager-specific logic.
4. App detects missing Codex and installs it.
5. App prevents duplicate setup runs.
6. App prevents duplicate service runs.
7. App detects stale locks and interrupted setup state.
8. App presents self-heal choices when conflicts are found.
9. Self-heal includes at least `kill` and `ignore`.
10. App supports one Telegram user/chat only.
11. Once paired, other Telegram users cannot take control.
12. App can recover from an interrupted prior setup without corrupting config.
13. App never launches a second Codex child while one is already owned by the service.
14. App keeps recovery/audit logs for kill/ignore/resume decisions.

---

# Explicit Build Notes for the Implementing AI

* V1 is **single-user**, not multi-user.
* Target only **Linux and macOS**.
* Implement **package-manager-aware npm installation**.
* Protect both **setup mode** and **service mode** with separate locks.
* Add **stale lock detection** and **interactive recovery**.
* Prefer asking the operator over silently killing processes.
* Never kill an uncertain process match.
* Keep all recovery flows explicit and logged.

---

# Recommended Terminal Prompts

## Duplicate live app found

`Another app instance appears to be running.`
Show PID, age, mode.

Choices:

* `kill` — terminate the existing instance
* `ignore` — continue without touching it
* `exit`

## Interrupted setup found

`A previous setup did not finish.`
Show completed steps.

Choices:

* `resume`
* `restart`
* `ignore`
* `exit`

## Orphaned Codex found

`A Codex process from a previous run may still be active.`

Choices:

* `kill`
* `ignore`
* `exit`
