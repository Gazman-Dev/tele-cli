from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import queue
import re

from core.json_store import load_json, save_json
from core.models import AuthState, Config
from core.paths import AppPaths

from .app_server_client import AppServerClient
from .app_server_process import SubprocessJsonRpcTransport
from .instructions import ensure_instruction_files, build_instruction_paths, lesson_path, load_lesson_texts
from .jsonrpc import JsonRpcClient, JsonRpcRequest, JsonRpcTransport
from .session_store import SessionStore
from .workspaces import WorkspaceManager

SLEEP_AI_TIMEOUT_SECONDS = 30.0


@dataclass
class SleepState:
    last_completed_at: str | None = None
    last_scheduled_for: str | None = None
    last_attempted_for: str | None = None
    generation: int = 0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "SleepState":
        return cls(
            last_completed_at=data.get("last_completed_at"),
            last_scheduled_for=data.get("last_scheduled_for"),
            last_attempted_for=data.get("last_attempted_for"),
            generation=int(data.get("generation", 0)),
        )


def load_sleep_state(paths: AppPaths) -> SleepState:
    return load_json(paths.sleep_state, SleepState.from_dict) or SleepState()


def save_sleep_state(paths: AppPaths, state: SleepState) -> None:
    save_json(paths.sleep_state, state.to_dict())


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def latest_sleep_deadline(now: datetime, hour_local: int) -> datetime:
    candidate = now.replace(hour=hour_local, minute=0, second=0, microsecond=0)
    if now < candidate:
        candidate -= timedelta(days=1)
    return candidate


def should_run_sleep(paths: AppPaths, now: datetime, hour_local: int) -> bool:
    deadline = latest_sleep_deadline(now, hour_local)
    state = load_sleep_state(paths)
    last_scheduled = _parse_iso(state.last_scheduled_for)
    if last_scheduled is not None and last_scheduled >= deadline:
        return False
    last_attempted = _parse_iso(state.last_attempted_for)
    return last_attempted is None or last_attempted < deadline


def _read_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [line.rstrip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def has_pending_sleep_work(paths: AppPaths) -> bool:
    instruction_paths = ensure_instruction_files(paths)
    for path in instruction_paths.session_memory_dir.glob("*.short_memory.md"):
        if _read_lines(path):
            return True
    return False


def _extract_latest_agent_message(thread_payload: dict) -> str | None:
    thread = thread_payload.get("thread")
    if not isinstance(thread, dict):
        return None
    turns = thread.get("turns")
    if not isinstance(turns, list):
        return None
    for turn in reversed(turns):
        if not isinstance(turn, dict):
            continue
        items = turn.get("items")
        if not isinstance(items, list):
            continue
        for item in reversed(items):
            if not isinstance(item, dict) or item.get("type") != "agentMessage":
                continue
            text = item.get("text")
            if isinstance(text, str) and text.strip():
                return text.strip()
            if isinstance(text, list):
                parts = [part for part in text if isinstance(part, str) and part.strip()]
                if parts:
                    return "\n".join(parts).strip()
    return None


def _build_sleep_prompt(
    *,
    current_long_memory: str,
    rules_text: str,
    personality_text: str,
    session_entries: list[tuple[str, list[str]]],
    deadline_label: str,
) -> str:
    short_memory_sections: list[str] = []
    for session_id, lines in session_entries:
        if not lines:
            continue
        short_memory_sections.append(f"## Session {session_id}\n" + "\n".join(lines))
    session_short_memory_text = "\n\n".join(short_memory_sections) if short_memory_sections else "No session short memory entries."
    return (
        "You are maintaining Tele Cli memory during the daily sleep cycle.\n\n"
        "Update the durable long memory and create today's lesson.\n"
        "Return valid JSON with exactly two keys: long_memory and lesson.\n"
        "Each value must be markdown text.\n"
        "Do not include any extra prose outside the JSON object.\n\n"
        f"Day: {deadline_label}\n\n"
        "Rules:\n"
        f"{rules_text}\n\n"
        "Personality:\n"
        f"{personality_text}\n\n"
        "Current long memory:\n"
        f"{current_long_memory}\n\n"
        "Session short memory entries:\n"
        f"{session_short_memory_text}\n"
    )


def _coerce_json_object(text: str) -> dict:
    stripped = text.strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", stripped, re.DOTALL)
        if not match:
            raise
        return json.loads(match.group(0))


def _run_sleep_ai(
    *,
    paths: AppPaths,
    config: Config,
    prompt: str,
    transport_factory,
    max_wait_seconds: float = SLEEP_AI_TIMEOUT_SECONDS,
) -> tuple[str, str]:
    transport: JsonRpcTransport = transport_factory(config, AuthState(bot_token="sleep"))
    rpc = JsonRpcClient(transport)
    rpc.start()
    client = AppServerClient(rpc)
    workspace_manager = WorkspaceManager(paths)
    workspace_root = workspace_manager.workspace_path(workspace_manager.ensure_workspace_initialized(workspace_manager.get_or_create_root_workspace().workspace_id))
    try:
        client.initialize("tele-cli-sleep")
        client.get_account()
        thread = client.thread_start(
            cwd=str(workspace_root),
            sandbox=config.sandbox_mode,
            approvalPolicy=config.approval_policy,
            personality=config.codex_personality,
        )
        thread_id = thread.get("threadId")
        if not thread_id:
            raise RuntimeError("Sleep thread did not receive a thread id.")
        turn = client.turn_start(
            thread_id,
            prompt,
            cwd=str(workspace_root),
            approvalPolicy=config.approval_policy,
            sandboxPolicy=config.sandbox_mode,
            personality=config.codex_personality,
        )
        turn_id = str(turn.get("turnId") or "")
        if not turn_id:
            raise RuntimeError("Sleep turn did not receive a turn id.")
        started_at = datetime.now(timezone.utc)
        while True:
            if (datetime.now(timezone.utc) - started_at).total_seconds() > max_wait_seconds:
                raise RuntimeError(f"Sleep AI timed out after {max_wait_seconds:.1f}s.")
            request: JsonRpcRequest | None = rpc.get_request_nowait()
            if request is not None:
                rpc.respond(request.id, {"approved": True})
            try:
                notification = rpc.get_notification(timeout=0.1)
            except queue.Empty:
                continue
            if notification.method not in {"turn/completed", "turn/failed"}:
                continue
            notified_turn_id = str((notification.params or {}).get("turnId") or "")
            if notified_turn_id != turn_id:
                continue
            if notification.method == "turn/failed":
                raise RuntimeError("Sleep AI turn failed.")
            break
        text = _extract_latest_agent_message(client.thread_read(thread_id, include_turns=True))
        if not text:
            raise RuntimeError("Sleep AI did not return any output.")
        payload = _coerce_json_object(text)
        long_memory = str(payload.get("long_memory") or "").strip()
        lesson = str(payload.get("lesson") or "").strip()
        if not long_memory or not lesson:
            raise RuntimeError("Sleep AI output did not contain both long_memory and lesson.")
        return long_memory, lesson
    finally:
        rpc.close()


def current_generation(paths: AppPaths) -> int:
    return load_sleep_state(paths).generation


def build_refresh_instructions(paths: AppPaths, session, *, max_lesson_chars: int = 6000, max_lesson_count: int = 3) -> tuple[str, int]:
    instruction_paths = ensure_instruction_files(paths)
    state = load_sleep_state(paths)
    if state.generation <= session.last_seen_generation:
        return "", session.last_seen_generation
    missed_lessons = load_lesson_texts(instruction_paths, session.last_seen_generation, state.generation)
    total_chars = sum(len(text) for _, _, text in missed_lessons)
    if missed_lessons and len(missed_lessons) <= max_lesson_count and total_chars <= max_lesson_chars:
        lesson_blocks = "\n\n".join(f"## {name}\n{text}" for _, name, text in missed_lessons if text)
        return (
            "Session refresh: apply the missed daily lessons before handling the new user request.\n\n"
            f"{lesson_blocks}".strip(),
            state.generation,
        )
    instruction_paths = build_instruction_paths(paths)
    full_refresh = (
        "Session refresh: your memory state was compacted during sleep. "
        "Use the current base files before handling the new user request.\n\n"
        "Rules:\n"
        f"{instruction_paths.rules.read_text(encoding='utf-8').strip() if instruction_paths.rules.exists() else ''}\n\n"
        "Personality:\n"
        f"{instruction_paths.personality.read_text(encoding='utf-8').strip() if instruction_paths.personality.exists() else ''}\n\n"
        "Long memory:\n"
        f"{instruction_paths.long_memory.read_text(encoding='utf-8').strip() if instruction_paths.long_memory.exists() else ''}"
    ).strip()
    return full_refresh, state.generation


def run_sleep(
    paths: AppPaths,
    config: Config,
    now: datetime | None = None,
    hour_local: int = 2,
    transport_factory=None,
    max_wait_seconds: float = SLEEP_AI_TIMEOUT_SECONDS,
) -> None:
    current = now or datetime.now().astimezone()
    instruction_paths = ensure_instruction_files(paths)
    deadline = latest_sleep_deadline(current, hour_local)
    state_before = load_sleep_state(paths)
    state_before.last_attempted_for = deadline.astimezone(timezone.utc).isoformat()
    save_sleep_state(paths, state_before)
    session_files = sorted(instruction_paths.session_memory_dir.glob("*.short_memory.md"))
    session_entries: list[tuple[str, list[str]]] = []
    for path in session_files:
        session_entries.append((path.stem.replace(".short_memory", ""), _read_lines(path)))

    current_long_memory = instruction_paths.long_memory.read_text(encoding="utf-8").strip() if instruction_paths.long_memory.exists() else ""
    rules_text = instruction_paths.rules.read_text(encoding="utf-8").strip() if instruction_paths.rules.exists() else ""
    personality_text = instruction_paths.personality.read_text(encoding="utf-8").strip() if instruction_paths.personality.exists() else ""
    long_memory_text, lesson_text = _run_sleep_ai(
        paths=paths,
        config=config,
        prompt=_build_sleep_prompt(
            current_long_memory=current_long_memory,
            rules_text=rules_text,
            personality_text=personality_text,
            session_entries=session_entries,
            deadline_label=deadline.date().isoformat(),
        ),
        transport_factory=transport_factory
        or (lambda cfg, auth: SubprocessJsonRpcTransport.start([*cfg.codex_command, "app-server", "--listen", "stdio://"])),
        max_wait_seconds=max_wait_seconds,
    )
    instruction_paths.long_memory.write_text(long_memory_text.strip() + "\n", encoding="utf-8")
    next_generation = state_before.generation + 1
    lesson_file = lesson_path(instruction_paths, next_generation, deadline.date().isoformat())
    lesson_file.write_text(lesson_text.strip() + "\n", encoding="utf-8")

    for path in session_files:
        path.write_text("", encoding="utf-8")

    store = SessionStore(paths)
    state = store.load()
    changed = False
    for session in state.sessions:
        if session.attached:
            session.instructions_dirty = True
            changed = True
    if changed:
        store.save(state)

    save_sleep_state(
        paths,
        SleepState(
            last_completed_at=current.astimezone(timezone.utc).isoformat(),
            last_scheduled_for=deadline.astimezone(timezone.utc).isoformat(),
            last_attempted_for=deadline.astimezone(timezone.utc).isoformat(),
            generation=next_generation,
        ),
    )
    workspace_manager = WorkspaceManager(paths)
    root_workspace = workspace_manager.ensure_workspace_initialized(workspace_manager.get_or_create_root_workspace().workspace_id)
    workspace_manager.commit_root_workspace_if_changed(f"Sleep memory update {deadline.date().isoformat()}")
    workspace_manager.best_effort_push_workspace(root_workspace)
