from __future__ import annotations

import unittest

from runtime.telegram_html import repair_partial_telegram_html


class TelegramHtmlUnitTests(unittest.TestCase):
    def test_repair_closes_nested_tags_in_order(self) -> None:
        self.assertEqual(
            repair_partial_telegram_html("<b><i>streaming"),
            "<b><i>streaming</i></b>",
        )

    def test_repair_closes_out_of_order_tags(self) -> None:
        self.assertEqual(
            repair_partial_telegram_html("<b><i>streaming</b>"),
            "<b><i>streaming</i></b>",
        )

    def test_repair_drops_unmatched_closing_tag(self) -> None:
        self.assertEqual(
            repair_partial_telegram_html("<b>streaming</i>"),
            "<b>streaming</b>",
        )

    def test_repair_strips_trailing_incomplete_tag_fragment(self) -> None:
        self.assertEqual(
            repair_partial_telegram_html("<b>streaming</b><i"),
            "<b>streaming</b>",
        )

    def test_repair_preserves_allowed_anchor_attributes_and_closes_anchor(self) -> None:
        self.assertEqual(
            repair_partial_telegram_html('<a href="https://example.com">docs'),
            '<a href="https://example.com">docs</a>',
        )

    def test_repair_escapes_unknown_tags(self) -> None:
        self.assertEqual(
            repair_partial_telegram_html("<marquee>nope</marquee>"),
            "&lt;marquee&gt;nope&lt;/marquee&gt;",
        )

    def test_repair_keeps_telegram_self_closing_tags_unclosed(self) -> None:
        self.assertEqual(
            repair_partial_telegram_html('<tg-emoji emoji-id="12345">'),
            '<tg-emoji emoji-id="12345">',
        )


if __name__ == "__main__":
    unittest.main()
