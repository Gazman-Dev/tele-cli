from __future__ import annotations

import html
import re


_ALLOWED_TAG_NAMES = {
    "b",
    "strong",
    "i",
    "em",
    "u",
    "ins",
    "s",
    "strike",
    "del",
    "code",
    "pre",
    "a",
    "blockquote",
    "tg-spoiler",
    "tg-emoji",
    "tg-time",
}
_HTML_TAG_TOKEN_RE = re.compile(r"(<[^>]+>)")
_ALLOWED_HTML_TAG_RE = re.compile(r"</?(?:b|strong|i|em|u|ins|s|strike|del|code|pre|a|blockquote|tg-spoiler|tg-emoji|tg-time)\b", re.IGNORECASE)
_COMMAND_ACTIVITY_PREFIX = "__tele_cli_command__:"
_LEGACY_DASH_VARIANTS = (
    "â€”",
    "â€“",
    "â€‘",
    "âˆ’",
)


def escape_telegram_html(text: str) -> str:
    return html.escape(text, quote=False)


def normalize_legacy_telegram_text(text: str) -> str:
    normalized = text
    for variant in _LEGACY_DASH_VARIANTS:
        normalized = normalized.replace(variant, "-")
    normalized = re.sub(r"\\([_*\[\]()~`>#+\-=|{}.!])", r"\1", normalized)
    return normalized.replace("\\\\", "\\")


def looks_like_telegram_html(text: str) -> bool:
    return bool(_ALLOWED_HTML_TAG_RE.search(text))


def repair_partial_telegram_html(text: str) -> str:
    normalized = (text or "").replace("\r\n", "\n").strip()
    if not normalized:
        return ""
    normalized = re.sub(r"<[^>\n]*$", "", normalized)
    pieces: list[str] = []
    stack: list[str] = []
    for token in _HTML_TAG_TOKEN_RE.split(normalized):
        if not token:
            continue
        if token.startswith("<") and token.endswith(">"):
            tag_match = re.match(r"^<\s*(/)?\s*([a-zA-Z0-9-]+)\b([^>]*)>$", token)
            if not tag_match:
                pieces.append(escape_telegram_html(token))
                continue
            is_closing = bool(tag_match.group(1))
            tag_name = tag_match.group(2).lower()
            suffix = tag_match.group(3) or ""
            if tag_name not in _ALLOWED_TAG_NAMES:
                pieces.append(escape_telegram_html(token))
                continue
            is_self_closing = suffix.strip().endswith("/") or tag_name in {"tg-emoji", "tg-time"}
            if is_closing:
                if tag_name in stack:
                    while stack:
                        open_tag = stack.pop()
                        pieces.append(f"</{open_tag}>")
                        if open_tag == tag_name:
                            break
                continue
            pieces.append(token)
            if not is_self_closing:
                stack.append(tag_name)
            continue
        pieces.append(token)
    while stack:
        pieces.append(f"</{stack.pop()}>")
    return "".join(pieces)


def _make_placeholder(index: int) -> str:
    return f"\ue000{index}\ue001"


def _restore_placeholders(text: str, replacements: list[str]) -> str:
    restored = text
    for index, replacement in enumerate(replacements):
        restored = restored.replace(_make_placeholder(index), replacement)
    return restored


def _replace_code_fences(text: str, replacements: list[str]) -> str:
    pattern = re.compile(r"```(?P<lang>[A-Za-z0-9_+-]*)\n(?P<body>.*?)```", re.DOTALL)

    def repl(match: re.Match[str]) -> str:
        language = match.group("lang").strip()
        code = escape_telegram_html(match.group("body").rstrip("\n"))
        if language:
            replacement = f'<pre><code class="language-{html.escape(language, quote=True)}">{code}</code></pre>'
        else:
            replacement = f"<pre><code>{code}</code></pre>"
        placeholder = _make_placeholder(len(replacements))
        replacements.append(replacement)
        return placeholder

    return pattern.sub(repl, text)


def _replace_inline_code(text: str, replacements: list[str]) -> str:
    pattern = re.compile(r"`([^`\n]+)`")

    def repl(match: re.Match[str]) -> str:
        replacement = f"<code>{escape_telegram_html(match.group(1))}</code>"
        placeholder = _make_placeholder(len(replacements))
        replacements.append(replacement)
        return placeholder

    return pattern.sub(repl, text)


def _replace_links(text: str, replacements: list[str]) -> str:
    pattern = re.compile(r"\[([^\]\n]+)\]\(([^)\n]+)\)")

    def repl(match: re.Match[str]) -> str:
        label = escape_telegram_html(match.group(1).strip())
        href = html.escape(match.group(2).strip(), quote=True)
        replacement = f'<a href="{href}">{label}</a>'
        placeholder = _make_placeholder(len(replacements))
        replacements.append(replacement)
        return placeholder

    return pattern.sub(repl, text)


def _apply_inline_markup(text: str) -> str:
    patterns = [
        (re.compile(r"\*\*(.+?)\*\*", re.DOTALL), r"<b>\1</b>"),
        (re.compile(r"(?<!\*)\*(?!\s)(.+?)(?<!\s)\*(?!\*)", re.DOTALL), r"<b>\1</b>"),
        (re.compile(r"__(.+?)__", re.DOTALL), r"<u>\1</u>"),
        (re.compile(r"_(.+?)_", re.DOTALL), r"<i>\1</i>"),
        (re.compile(r"~~(.+?)~~", re.DOTALL), r"<s>\1</s>"),
    ]
    rendered = text
    for pattern, replacement in patterns:
        rendered = pattern.sub(replacement, rendered)
    return rendered


def _render_line(line: str) -> str:
    stripped = line.strip()
    if not stripped:
        return ""
    heading = re.match(r"^(#{1,6})\s+(.*)$", stripped)
    if heading:
        return f"**{heading.group(2).strip()}**"
    bullet = re.match(r"^[-*]\s+(.*)$", stripped)
    if bullet:
        return f"• {bullet.group(1).strip()}"
    return stripped


def to_telegram_html(text: str) -> str:
    normalized = text.replace("\r\n", "\n").strip()
    if not normalized:
        return ""
    replacements: list[str] = []
    working = _replace_code_fences(normalized, replacements)
    working = _replace_inline_code(working, replacements)
    working = _replace_links(working, replacements)
    lines = [_render_line(line) for line in working.split("\n")]
    escaped = escape_telegram_html("\n".join(lines))
    escaped = _apply_inline_markup(escaped)
    return _restore_placeholders(escaped, replacements)


def _thinking_title_and_body(text: str | None) -> tuple[str, str]:
    body = (text or "").strip()
    if not body:
        return "Thinking", ""
    if body.startswith(_COMMAND_ACTIVITY_PREFIX):
        return "Running", body[len(_COMMAND_ACTIVITY_PREFIX) :].strip()
    return "Thinking", body


def render_telegram_progress_html(text: str | None) -> str:
    title, body = _thinking_title_and_body(text)
    if not body or body == title:
        return ""
    if title == "Running":
        command, _, output = body.partition("\n\n")
        command_html = escape_telegram_html(command)
        rendered = f"<pre><code class=\"language-bash\">{command_html}</code></pre>"
        if output.strip():
            rendered = f"{rendered}\n\n{to_telegram_html(output)}"
        return rendered
    if looks_like_telegram_html(body):
        return repair_partial_telegram_html(body)
    return to_telegram_html(body)


def _strip_disallowed_collapsed_html(text: str) -> str:
    stripped = re.sub(r"<pre><code[^>]*>(.*?)</code></pre>", r"\1", text, flags=re.DOTALL | re.IGNORECASE)
    stripped = re.sub(r"</?code[^>]*>", "", stripped, flags=re.IGNORECASE)
    stripped = re.sub(r"</?(?:u|ins|s|strike|del|tg-spoiler|tg-emoji|tg-time)[^>]*>", "", stripped, flags=re.IGNORECASE)
    return stripped


def _truncate_collapsed_html(text: str, max_chars: int) -> str:
    normalized = text.strip()
    if len(normalized) <= max_chars:
        return normalized
    truncated = normalized[: max_chars - 1].rstrip()
    split_at = truncated.rfind("\n\n")
    if split_at > max_chars // 2:
        truncated = truncated[:split_at].rstrip()
    return f"{truncated}…"


def render_collapsed_thinking_html(
    thinking_history: str | list[str] | None,
    *,
    max_chars: int = 1200,
) -> str:
    if isinstance(thinking_history, list):
        thinking_entries = [entry.strip() for entry in thinking_history if isinstance(entry, str) and entry.strip()]
    else:
        thinking_entries = [line.strip() for line in (thinking_history or "").split("\n") if line.strip()]
    if not thinking_entries:
        return ""
    rendered_entries = [_strip_disallowed_collapsed_html(render_telegram_progress_html(entry)) for entry in thinking_entries]
    thinking_body = "\n\n".join(entry for entry in rendered_entries if entry.strip())
    if not thinking_body:
        return ""
    thinking_body = _truncate_collapsed_html(thinking_body, max_chars=max_chars)
    return f"<blockquote expandable>{thinking_body}</blockquote>"


def render_final_telegram_html(*, answer_text: str, thinking_history_text: str | None) -> str:
    answer_html = answer_text.strip() if looks_like_telegram_html(answer_text) else to_telegram_html(answer_text)
    thinking_html = render_collapsed_thinking_html(thinking_history_text)
    if not thinking_html:
        return answer_html
    return f"{thinking_html}\n\n{answer_html}"
