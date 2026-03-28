from __future__ import annotations

import unittest

from runtime.telegram_markdown import escape_telegram_markdown_v2, to_telegram_markdown_v2


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

    def test_preserves_links(self) -> None:
        text = "See [OpenAI](https://openai.com/docs)."
        rendered = to_telegram_markdown_v2(text)
        self.assertEqual(rendered, "See [OpenAI](https://openai.com/docs)\\.")

    def test_malformed_markdown_degrades_safely(self) -> None:
        text = "**bold\n[broken](not-a-url)"
        rendered = to_telegram_markdown_v2(text)
        self.assertEqual(rendered, "\\*\\*bold\n\\[broken\\]\\(not\\-a\\-url\\)")

    def test_escape_telegram_markdown_v2_escapes_plain_text_without_formatting(self) -> None:
        text = "# Title\n**bold**"
        rendered = escape_telegram_markdown_v2(text)
        self.assertEqual(rendered, "\\# Title\n\\*\\*bold\\*\\*")


if __name__ == "__main__":
    unittest.main()
