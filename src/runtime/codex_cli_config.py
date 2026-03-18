from __future__ import annotations

from pathlib import Path
import re


_ASSIGNMENT_RE = re.compile(r'^\s*(model|model_reasoning_effort)\s*=\s*"([^"]*)"\s*$')
_UNSET = object()


def codex_cli_config_path() -> Path:
    return Path.home().joinpath(".codex", "config.toml")


def read_codex_cli_preferences(path: Path | None = None) -> tuple[str | None, str | None]:
    target = path or codex_cli_config_path()
    if not target.exists():
        return None, None
    model: str | None = None
    reasoning: str | None = None
    for line in target.read_text(encoding="utf-8").splitlines():
        match = _ASSIGNMENT_RE.match(line)
        if not match:
            continue
        key, value = match.groups()
        if key == "model":
            model = value
        elif key == "model_reasoning_effort":
            reasoning = value
    return model, reasoning


def write_codex_cli_preferences(
    *,
    path: Path | None = None,
    model=_UNSET,
    reasoning=_UNSET,
) -> tuple[str | None, str | None]:
    target = path or codex_cli_config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    original_lines = target.read_text(encoding="utf-8").splitlines() if target.exists() else []
    replacements: dict[str, str] = {}
    if model is not _UNSET:
        replacements["model"] = str(model)
    if reasoning is not _UNSET:
        replacements["model_reasoning_effort"] = str(reasoning)

    written: set[str] = set()
    output_lines: list[str] = []
    for line in original_lines:
        match = _ASSIGNMENT_RE.match(line)
        if not match:
            output_lines.append(line)
            continue
        key, _ = match.groups()
        if key in replacements:
            if key in written:
                continue
            output_lines.append(f'{key} = "{replacements[key]}"')
            written.add(key)
            continue
        if key in written:
            continue
        output_lines.append(line)
        written.add(key)
    for key in ("model", "model_reasoning_effort"):
        if key in replacements and key not in written:
            output_lines.append(f'{key} = "{replacements[key]}"')
    text = "\n".join(output_lines).rstrip()
    target.write_text((text + "\n") if text else "", encoding="utf-8")
    return read_codex_cli_preferences(target)
