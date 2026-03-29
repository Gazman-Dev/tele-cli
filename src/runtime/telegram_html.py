from __future__ import annotations

import html
import re


_PLACEHOLDER_PREFIX = "\u0000tele_cli_html_"
_ALLOWED_HTML_TAG_RE = re.compile(r"</?(?:b|strong|i|em|u|ins|s|strike|del|code|pre|a|blockquote|tg-spoiler|tg-emoji|tg-time)\b", re.IGNORECASE)


def escape_telegram_html(text: str) -> str:
    return html.escape(text, quote=False)


def looks_like_telegram_html(text: str) -> bool:
    return bool(_ALLOWED_HTML_TAG_RE.search(text))


def _make_placeholder(index: int) -> str:
    return f"{_PLACEHOLDER_PREFIX}{index}\u0000"


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
    if body.startswith("__tele_cli_command__:"):
        return "Running", body.split(":", 1)[1].strip()
    return "Thinking", body


def render_telegram_progress_html(text: str | None) -> str:
    title, body = _thinking_title_and_body(text)
    if not body:
        return f"<b>{title}</b>"
    if body == title:
        return f"<b>{title}</b>"
    if title == "Running":
        command_html = escape_telegram_html(body)
        return f"<b>{title}</b>\n\n<pre><code class=\"language-bash\">{command_html}</code></pre>"
    return f"<b>{title}</b>\n\n{to_telegram_html(body)}"


def render_collapsed_thinking_html(thinking_history_text: str | None) -> str:
    thinking_lines = [line.strip() for line in (thinking_history_text or "").split("\n") if line.strip()]
    if not thinking_lines:
        return ""
    rendered_entries = [render_telegram_progress_html(line) for line in thinking_lines]
    thinking_body = "\n\n".join(entry for entry in rendered_entries if entry.strip())
    if not thinking_body:
        return ""
    return f"<b>Thinking</b>\n<blockquote expandable>{thinking_body}</blockquote>"


def render_final_telegram_html(*, answer_text: str, thinking_history_text: str | None) -> str:
    answer_html = answer_text.strip() if looks_like_telegram_html(answer_text) else to_telegram_html(answer_text)
    thinking_html = render_collapsed_thinking_html(thinking_history_text)
    if not thinking_html:
        return answer_html
    return f"{thinking_html}\n\n{answer_html}"
