# Memory Process

`tele-cli` uses a layered memory model with an exclusive daily sleep phase.

## Files

- `rules.md`
  High-priority operating rules. Keep this short.
- `personality.md`
  Response style and behavioral preferences.
- `long_memory.md`
  Durable memory maintained by sleep.
- `lessons/`
  Daily lesson files written by sleep. Each file is versioned by generation and date.
- `~/.tele-cli/memory/sessions/<session_id>.short_memory.md`
  Append-only short memory for one session.
- `~/.tele-cli/sleep_state.json`
  Tracks the last completed sleep cycle and the current memory generation.

## Session Start

When a session starts, `tele-cli` injects:

- `rules.md`
- `personality.md`
- `long_memory.md`
- the latest lesson file, if one exists

The startup instructions also tell the agent about that session's append-only short-memory file.

## Short Memory

Each session has exactly one short-memory file.

Rules:

- it is append-only while the system is active
- it is shared state in the sense that all session files live in the same managed memory area
- it is temporary working memory, not durable memory
- it is cleared by sleep

## Sleep

Sleep is an exclusive AI maintenance phase.

During sleep:

- normal message processing is paused
- incoming messages remain queued
- all session short-memory files are collected as input
- AI updates `long_memory.md`
- AI writes one new lesson file in `lessons/`
- all session short-memory files are cleared
- all attached sessions are marked tired (`instructions_dirty = true`)

Sleep runs:

- on startup if the most recent scheduled sleep window was missed
- during normal runtime when the next scheduled sleep window is reached

## Tired Sessions

After sleep, sessions are not refreshed proactively.

Refresh is lazy:

- if a tired session receives a new user message, `tele-cli` refreshes it first
- if the session missed only a small number of lesson files, `tele-cli` injects the missed lessons
- if the missed lesson set is too large, `tele-cli` injects the current `rules.md`, `personality.md`, and `long_memory.md` instead

After a successful refreshed turn starts, that session is marked caught up to the current memory generation.
