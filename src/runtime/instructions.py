from __future__ import annotations

from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

from core.json_store import load_json
from core.models import AuthState
from core.paths import AppPaths


@dataclass(frozen=True)
class InstructionPaths:
    workspace_root: Path
    system_dir: Path
    template: Path
    refresh_template: Path
    sleep_template: Path
    defaults_dir: Path
    personality: Path
    rules: Path
    agents_md: Path
    long_memory: Path
    lessons_dir: Path
    session_memory_dir: Path


DEFAULT_FILE_MAP = {
    "personality.md": "personality.md",
    "rules.md": "rules.md",
    "long_memory.md": "long_memory.md",
}

RESOURCE_PACKAGE = "runtime.resources"
RESOURCE_TEMPLATE_MAP = {
    "session_instructions.md": "session_instructions.md",
    "refresh_instructions.md": "refresh_instructions.md",
    "sleep_prompt.md": "sleep_prompt.md",
}
RESOURCE_DEFAULT_MAP = {
    "personality.md": "defaults/personality.md",
    "rules.md": "defaults/rules.md",
    "long_memory.md": "defaults/long_memory.md",
}
SEEDED_MARKER = ".seeded-defaults-v1"


def build_instruction_paths(paths: AppPaths) -> InstructionPaths:
    workspace_root = paths.workspace
    system_dir = paths.root / "system"
    defaults_dir = system_dir / "defaults"
    return InstructionPaths(
        workspace_root=workspace_root,
        system_dir=system_dir,
        template=system_dir / "session_instructions.md",
        refresh_template=system_dir / "refresh_instructions.md",
        sleep_template=system_dir / "sleep_prompt.md",
        defaults_dir=defaults_dir,
        personality=paths.root / "personality.md",
        rules=paths.root / "rules.md",
        agents_md=workspace_root / "AGENTS.md",
        long_memory=workspace_root / "long_memory.md",
        lessons_dir=paths.lessons,
        session_memory_dir=paths.session_memory,
    )


def session_short_memory_relpath(session_id: str) -> str:
    return f"memory/sessions/{session_id}.short_memory.md"


def session_short_memory_path(paths: AppPaths, session_id: str) -> Path:
    return paths.root / session_short_memory_relpath(session_id)


def telegram_session_name(paths: AppPaths, session) -> str:
    auth = load_json(paths.auth, AuthState.from_dict)
    if (
        auth is not None
        and auth.telegram_chat_id is not None
        and session.transport_chat_id == auth.telegram_chat_id
        and session.transport_topic_id is None
    ):
        return "main"
    if session.transport_chat_id is None:
        return "telegram"
    if session.transport_topic_id is not None:
        return f"{session.transport_chat_id}/{session.transport_topic_id}"
    return str(session.transport_chat_id)


def session_name(paths: AppPaths, session) -> str:
    if session.transport == "local":
        return (session.transport_channel or "main").strip() or "main"
    if session.transport == "telegram":
        return telegram_session_name(paths, session)
    return session.session_id


def _read_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8").strip()


def _read_resource_text(resource_path: str) -> str:
    resource = files(RESOURCE_PACKAGE)
    for part in resource_path.split("/"):
        resource = resource.joinpath(part)
    return resource.read_text(encoding="utf-8")


def ensure_instruction_files(paths: AppPaths) -> InstructionPaths:
    instruction_paths = build_instruction_paths(paths)
    instruction_paths.workspace_root.mkdir(parents=True, exist_ok=True)
    instruction_paths.system_dir.mkdir(parents=True, exist_ok=True)
    instruction_paths.defaults_dir.mkdir(parents=True, exist_ok=True)
    instruction_paths.session_memory_dir.mkdir(parents=True, exist_ok=True)
    instruction_paths.lessons_dir.mkdir(parents=True, exist_ok=True)
    seeded_marker = instruction_paths.system_dir / SEEDED_MARKER
    should_seed_state_defaults = not seeded_marker.exists()
    for target_name, resource_name in RESOURCE_TEMPLATE_MAP.items():
        target = instruction_paths.system_dir / target_name
        if target.exists():
            continue
        target.write_text(_read_resource_text(resource_name), encoding="utf-8")
    for target_name, resource_name in RESOURCE_DEFAULT_MAP.items():
        default_target = instruction_paths.defaults_dir / target_name
        if not default_target.exists():
            default_target.write_text(_read_resource_text(resource_name), encoding="utf-8")
        target = getattr(instruction_paths, target_name.replace(".md", ""))
        if target.exists() and (not should_seed_state_defaults or target.read_text(encoding="utf-8").strip()):
            continue
        target.write_text(_read_resource_text(resource_name), encoding="utf-8")
    if should_seed_state_defaults:
        seeded_marker.write_text("seeded\n", encoding="utf-8")
    return instruction_paths


def lesson_path(instruction_paths: InstructionPaths, generation: int, day_label: str) -> Path:
    return instruction_paths.lessons_dir / f"{generation:04d}-{day_label}.md"


def load_lesson_texts(instruction_paths: InstructionPaths, generation_start_exclusive: int, generation_end_inclusive: int) -> list[tuple[int, str, str]]:
    items: list[tuple[int, str, str]] = []
    for path in sorted(instruction_paths.lessons_dir.glob("*.md")):
        stem = path.stem
        prefix = stem.split("-", 1)[0]
        try:
            generation = int(prefix)
        except ValueError:
            continue
        if generation_start_exclusive < generation <= generation_end_inclusive:
            items.append((generation, path.name, _read_text(path)))
    return items


def render_session_instructions(paths: AppPaths, session, refresh_reason: str = "session_start") -> str:
    instruction_paths = ensure_instruction_files(paths)
    template = _read_text(instruction_paths.template)
    latest_lessons = load_lesson_texts(instruction_paths, -1, 10**9)
    latest_lesson_text = latest_lessons[-1][2] if latest_lessons else ""
    replacements = {
        "{{refresh_reason}}": refresh_reason,
        "{{session_name}}": session_name(paths, session),
        "{{rules}}": _read_text(instruction_paths.rules),
        "{{personality}}": _read_text(instruction_paths.personality),
        "{{long_memory}}": _read_text(instruction_paths.long_memory),
        "{{lessons}}": latest_lesson_text,
        "{{session_short_memory_path}}": session_short_memory_relpath(session.session_id),
    }
    rendered = template
    for placeholder, value in replacements.items():
        rendered = rendered.replace(placeholder, value)
    return rendered.strip()
