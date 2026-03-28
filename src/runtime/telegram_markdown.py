from __future__ import annotations

import re


_SPECIAL_CHARS = set("\\_*[]()~`>#+-=|{}.!")
_DASH_TRANSLATION = str.maketrans({
    "—": "-",
    "–": "-",
    "‑": "-",
    "−": "-",
})


def _escape_plain(text: str) -> str:
    escaped: list[str] = []
    for char in text:
        if char in _SPECIAL_CHARS:
            escaped.append("\\" + char)
        else:
            escaped.append(char)
    return "".join(escaped)


def escape_telegram_markdown_v2(text: str) -> str:
    return _escape_plain(text)


def normalize_existing_telegram_markdown_v2(text: str) -> str:
    normalized = text.translate(_DASH_TRANSLATION)
    lines: list[str] = []
    for line in normalized.splitlines():
        updated = line
        updated = re.sub(r"^(\s*)-\s", r"\1\\- ", updated)
        updated = updated.replace(" - ", " \\- ")
        lines.append(updated)
    return "\n".join(lines)


def normalize_telegram_markdown_source(text: str) -> str:
    normalized = text.translate(_DASH_TRANSLATION)
    normalized = re.sub(r"\\([_*\[\]()~`>#+\-=|{}.!])", r"\1", normalized)
    normalized = normalized.replace("\\\\", "\\")
    return normalized


def safe_stream_markdown_v2(text: str) -> str:
    normalized = normalize_telegram_markdown_source(text)
    return escape_telegram_markdown_v2(normalized)


def _escape_code(text: str) -> str:
    return text.replace("\\", "\\\\").replace("`", "\\`")


def code_block_telegram_markdown_v2(text: str, language: str = "") -> str:
    body = _escape_code(text)
    prefix = f"```{language}\n" if language else "```\n"
    return f"{prefix}{body}\n```"


def _escape_link_url(url: str) -> str:
    return url.replace("\\", "\\\\").replace(")", "\\)")


def _apply_line_level_rules(text: str) -> str:
    lines: list[str] = []
    for line in text.splitlines():
        stripped = line.lstrip()
        indent = line[: len(line) - len(stripped)]
        heading = re.match(r"^(#{1,6})\s+(.+)$", stripped)
        if heading:
            lines.append(f"{indent}**{heading.group(2).strip()}**")
            continue
        lines.append(line)
    return "\n".join(lines)


def to_telegram_markdown_v2(text: str) -> str:
    normalized = _apply_line_level_rules(text)
    placeholders: list[str] = []

    def stash(rendered: str) -> str:
        placeholders.append(rendered)
        return f"\x00{len(placeholders) - 1}\x00"

    def replace_fence(match: re.Match[str]) -> str:
        language = (match.group(1) or "").strip()
        body = match.group(2)
        if body.endswith("\n"):
            body = body[:-1]
        body = _escape_code(body)
        prefix = f"```{language}\n" if language else "```\n"
        return stash(f"{prefix}{body}\n```")

    normalized = re.sub(r"```([A-Za-z0-9_+-]*)\n(.*?)```", replace_fence, normalized, flags=re.DOTALL)

    def replace_inline_code(match: re.Match[str]) -> str:
        return stash(f"`{_escape_code(match.group(1))}`")

    normalized = re.sub(r"`([^`\n]+)`", replace_inline_code, normalized)

    def replace_link(match: re.Match[str]) -> str:
        label = _escape_plain(match.group(1))
        url = _escape_link_url(match.group(2).strip())
        return stash(f"[{label}]({url})")

    normalized = re.sub(r"\[([^\]\n]+)\]\((https?://[^\s)]+(?:\)[^\s)]*)?)\)", replace_link, normalized)

    def replace_bold(match: re.Match[str]) -> str:
        return stash(f"*{_escape_plain(match.group(1))}*")

    normalized = re.sub(r"(?<!\*)\*\*([^\n*][^*]*?)\*\*(?!\*)", replace_bold, normalized)
    normalized = re.sub(r"(?<!\*)\*([^\n*][^*]*?)\*(?!\*)", replace_bold, normalized)

    def replace_strike(match: re.Match[str]) -> str:
        return stash(f"~{_escape_plain(match.group(1))}~")

    normalized = re.sub(r"~~([^\n~][^~]*?)~~", replace_strike, normalized)

    def replace_italic(match: re.Match[str]) -> str:
        return stash(f"_{_escape_plain(match.group(1))}_")

    normalized = re.sub(r"(?<![A-Za-z0-9_])_([^_\n]+?)_(?![A-Za-z0-9_])", replace_italic, normalized)

    escaped = escape_telegram_markdown_v2(normalized)

    def restore(match: re.Match[str]) -> str:
        return placeholders[int(match.group(1))]

    return re.sub(r"\x00(\d+)\x00", restore, escaped)
