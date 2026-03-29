from __future__ import annotations

import unittest

from runtime.telegram_markdown import (
    code_block_telegram_markdown_v2,
    escape_telegram_markdown_v2,
    normalize_existing_telegram_markdown_v2,
    normalize_telegram_markdown_source,
    safe_stream_markdown_v2,
    to_telegram_markdown_v2,
)


class TelegramMarkdownTests(unittest.TestCase):
    def test_escapes_reserved_characters(self) -> None:
        text = "_ * [ ] ( ) ~ ` > # + - = | { } . !"
        rendered = to_telegram_markdown_v2(text)
        self.assertEqual(rendered, "\\_ \\* \\[ \\] \\( \\) \\~ \\` \\> \\# \\+ \\- \\= \\| \\{ \\} \\. \\!")

    def test_preserves_fenced_code_blocks(self) -> None:
        text = "```python\nprint('hi')\n```"
        rendered = to_telegram_markdown_v2(text)
        self.assertEqual(rendered, "```python\nprint('hi')\n```")

    def test_preserves_inline_code(self) -> None:
        text = "Use `pip install tele-cli` now."
        rendered = to_telegram_markdown_v2(text)
        self.assertEqual(rendered, "Use `pip install tele-cli` now\\.")

    def test_converts_headings_and_bold_and_italic(self) -> None:
        text = "# Title\n**bold** and _italic_"
        rendered = to_telegram_markdown_v2(text)
        self.assertEqual(rendered, "*Title*\n*bold* and _italic_")

    def test_preserves_single_asterisk_bold(self) -> None:
        text = "*bold* and _italic_"
        rendered = to_telegram_markdown_v2(text)
        self.assertEqual(rendered, "*bold* and _italic_")

    def test_preserves_links(self) -> None:
        text = "See [OpenAI](https://openai.com/docs)."
        rendered = to_telegram_markdown_v2(text)
        self.assertEqual(rendered, "See [OpenAI](https://openai.com/docs)\\.")

    def test_preserves_file_links_with_line_anchors(self) -> None:
        text = "See [job.ts](/Users/ilyagazman/git/websites/scripts/eval-loop/job.ts#L123)."
        rendered = to_telegram_markdown_v2(text)
        self.assertEqual(
            rendered,
            "See [job\\.ts](/Users/ilyagazman/git/websites/scripts/eval-loop/job.ts#L123)\\.",
        )

    def test_malformed_markdown_degrades_safely(self) -> None:
        text = "**bold\n[broken](not-a-url)"
        rendered = to_telegram_markdown_v2(text)
        self.assertEqual(rendered, "\\*\\*bold\n\\[broken\\]\\(not\\-a\\-url\\)")

    def test_escape_telegram_markdown_v2_escapes_plain_text_without_formatting(self) -> None:
        text = "# Title\n**bold**"
        rendered = escape_telegram_markdown_v2(text)
        self.assertEqual(rendered, "\\# Title\n\\*\\*bold\\*\\*")

    def test_escape_telegram_markdown_v2_escapes_backslashes(self) -> None:
        text = r"Path C:\temp\file.txt"
        rendered = escape_telegram_markdown_v2(text)
        self.assertEqual(rendered, r"Path C:\\temp\\file\.txt")

    def test_code_block_telegram_markdown_v2_wraps_multiline_text(self) -> None:
        text = "hello\n`code`"
        rendered = code_block_telegram_markdown_v2(text)
        self.assertEqual(rendered, "```\nhello\n\\`code\\`\n```")

    def test_normalize_existing_telegram_markdown_v2_fixes_dashes_and_bullets(self) -> None:
        text = "I’m *Tele Cli* — your Telegram\\-first assistant.\n- one\n- two"
        rendered = normalize_existing_telegram_markdown_v2(text)
        self.assertEqual(rendered, "I’m *Tele Cli* \\- your Telegram\\-first assistant.\n\\- one\n\\- two")

    def test_normalize_telegram_markdown_source_unescapes_existing_telegram_escapes(self) -> None:
        text = "Telegram\\-first\n\\- bullet\n\\*not bold\\*"
        rendered = normalize_telegram_markdown_source(text)
        self.assertEqual(rendered, "Telegram-first\n- bullet\n*not bold*")

    def test_safe_stream_markdown_v2_escapes_partial_text_conservatively(self) -> None:
        text = "Hello *world - ok!"
        rendered = safe_stream_markdown_v2(text)
        self.assertEqual(rendered, "Hello *world \\- ok\\!*")

    def test_safe_stream_markdown_v2_closes_partial_inline_code(self) -> None:
        text = "Use `pip install"
        rendered = safe_stream_markdown_v2(text)
        self.assertEqual(rendered, "Use `pip install`")

    def test_safe_stream_markdown_v2_closes_partial_fenced_code_block(self) -> None:
        text = "```python\nprint('hi')"
        rendered = safe_stream_markdown_v2(text)
        self.assertEqual(rendered, "```python\nprint('hi')\n```")

    def test_safe_stream_markdown_v2_keeps_headings_readable(self) -> None:
        text = "# Title"
        rendered = safe_stream_markdown_v2(text)
        self.assertEqual(rendered, "*Title*")


if __name__ == "__main__":
    unittest.main()
