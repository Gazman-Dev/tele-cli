# Tele-Cli CLI UX Specification

## 1. Product Identity

**Tele-Cli** is a lightweight background service that connects:

```
Telegram ↔ Tele-Cli ↔ Codex
```

Tele-Cli runs locally and exposes control via Telegram.

The CLI is only used for:

* installation
* setup
* status
* debugging
* lifecycle control

Once configured, the CLI should rarely be opened.

---

# 2. Installation Experience

Tele-Cli supports two entry paths.

### Method A — Remote Install

```
curl -s https://github.com/<repo>/install.sh | bash
```

Installer performs automatically:

1. Download Tele-Cli
2. Install dependencies
3. Create config directory
4. Register `tele-cli` command
5. Start Tele-Cli service
6. Launch setup flow if needed

Installer output should be minimal:

```
Installing Tele-Cli...

Installing dependencies
Installing Codex
Creating configuration
Starting Tele-Cli service

Installation complete.
```

If setup is required, Tele-Cli launches immediately.

---

### Method B — Local Command

```
tele-cli
```

This opens the **main status screen**.

---

# 3. Dependency Handling

Tele-Cli must **never ask permission** for dependencies.

If a dependency is missing:

```
Checking dependencies...

Installing Codex
Installing npm
Installing required Python packages
```

All required tools are installed automatically.

Failures produce a clear error:

```
Dependency installation failed: Codex

Check internet connection and run:

tele-cli setup
```

---

# 4. Tele-Cli Service Behavior

Tele-Cli runs as a **background service**.

On launch it checks:

```
Is service already running?
```

Possible outcomes:

### Case 1 — Service running

```
Tele-Cli service already running.
```

User enters the status screen.

---

### Case 2 — Service blocked by another instance

Example:

```
Another Tele-Cli instance detected (PID 8421)

Kill the running instance and start a new one?
```

Options:

```
Kill and restart
Cancel
```

If confirmed:

```
Stopping previous instance...
Starting Tele-Cli...
```

---

# 5. Main Status Screen

The main screen is a **status dashboard**.

Displayed when running:

```
tele-cli
```

Information shown:

```
Tele-Cli v0.1.0
────────────────────────

Service: running
Codex: authenticated / not authenticated
Telegram: paired / not paired

Status: <one-line error or message>
```

Examples:

```
Status: waiting for Telegram commands
```

or

```
Status: Telegram connection lost
```

or

```
Status: Codex authentication required
```

---

# 6. Setup Flow

If configuration is missing:

```
Configuration required
Launching setup
```

Setup consists of two screens:

1. Telegram token
2. Telegram pairing

---

# 7. Consistent Input Screen Pattern

All user input screens share the same structure:

```
Top section:
  Explanation
  Instructions
  Helpful links

Bottom section:
  Text input field
```

Input area always stays at the bottom of the screen.

This creates a **consistent mental model** for entering data.

---

# 8. Telegram Token Setup Screen

Full-screen step.

Top section explains the process clearly.

Example content:

```
Telegram Bot Setup
────────────────────────

Tele-Cli requires a Telegram bot token.

Steps to obtain it:

1. Open Telegram
2. Search for: BotFather
3. Send: /newbot
4. Choose a name
5. Copy the bot token provided

Paste the token below.
```

Bottom input field:

```
Bot Token:
>
```

Validation occurs immediately after submission.

If invalid:

```
Invalid Telegram token.

Please paste the token exactly as provided by BotFather.
```

If valid:

```
Telegram token saved.
```

Proceed to pairing.

---

# 9. Pairing Screen

Second full-screen setup step.

Tele-Cli generates a pairing code.

Top section:

```
Telegram Pairing
────────────────────────

To connect this machine:

1. Open your Telegram bot
2. Send the following command:

/pair <code>

Pairing code:
  738214
```

Bottom input area shows progress:

```
Waiting for pairing confirmation...
```

Once Telegram confirms:

```
Device successfully paired.
```

Setup completes.

---

# 10. Setup Completion

After pairing:

```
Setup complete.
Starting Tele-Cli service...
```

User returns automatically to the **main status screen**.

---

# 11. Debug Mode

Debug mode focuses only on **Codex interaction**.

Entering debug mode launches a **full-screen passthrough view** of Codex.

Important:

Codex is an **interactive terminal application**, not STDOUT logs.

Therefore debug mode simply **mirrors the Codex terminal session**.

Behavior:

```
Debug Mode

Displaying Codex terminal session.
Press q to exit.
```

Tele-Cli does not alter the output.

It only shows the raw interface exactly as Codex presents it.

---

# 12. Logging Behavior

Logs are always enabled.

They are **not exposed in the CLI UI**.

Stored automatically in:

```
~/.tele-cli/logs/
```

Files:

```
tele-cli.log
telegram.log
codex.log
```

Logging properties:

* rotation enabled
* size limits enforced
* automatic cleanup

Logs are intended for **technical debugging only**.

Users access them via filesystem.

---

# 13. Auto Start Behavior

After installation:

```
Tele-Cli service automatically starts
```

On machine restart:

Tele-Cli should automatically start again.

If startup fails due to existing service:

```
Existing Tele-Cli instance detected.

Kill it and start Tele-Cli?
```

---

# 14. Status Indicators

The main screen shows three system states:

### Service

```
running
stopped
starting
```

---

### Codex

```
authenticated
not authenticated
installing
```

---

### Telegram

```
paired
not paired
connecting
```

---

# 15. Uninstall Flow

Uninstall must require **intent confirmation**.

User command:

```
tele-cli uninstall
```

Confirmation screen:

```
You are about to remove Tele-Cli.

This will delete:

~/.tele-cli
Tele-Cli service
tele-cli command

Type REMOVE to continue.
```

Input field:

```
>
```

Only the exact word **REMOVE** proceeds.

If entered:

```
Stopping Tele-Cli service
Removing files
Removing command

Tele-Cli successfully removed.
```

---

# 16. Version Visibility

Version should appear in:

* startup screen
* status screen
* command output

Command:

```
tele-cli --version
```

Output:

```
Tele-Cli 0.1.0
```

---

# 17. Error Philosophy

Errors must always appear as a **single line status message** on the main screen.

Examples:

```
Status: waiting for Telegram connection
```

```
Status: Codex authentication expired
```

```
Status: service restarting after crash
```

This prevents clutter while keeping users informed.

---

# 18. UX Principles

Tele-Cli CLI design follows these principles:

### 1. Zero permission friction

Dependencies install automatically.

### 2. Single responsibility screens

Each screen performs one task.

### 3. Clear system state

Service, Codex, and Telegram always visible.

### 4. Setup once

After setup, the CLI is rarely needed.

### 5. Transparent debugging

Codex debug mode shows the real session.