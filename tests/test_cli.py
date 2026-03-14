from __future__ import annotations

import unittest

from minic.cli import build_parser


class CliTests(unittest.TestCase):
    def test_parser_defaults_to_menu_mode(self) -> None:
        parser = build_parser()
        args = parser.parse_args([])
        self.assertIsNone(args.command)

    def test_parser_accepts_commands(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["menu"])
        self.assertEqual(args.command, "menu")

        args = parser.parse_args(["setup"])
        self.assertEqual(args.command, "setup")

        args = parser.parse_args(["service"])
        self.assertEqual(args.command, "service")

        args = parser.parse_args(["debug"])
        self.assertEqual(args.command, "debug")

        args = parser.parse_args(["reset-auth"])
        self.assertEqual(args.command, "reset-auth")

        args = parser.parse_args(["update"])
        self.assertEqual(args.command, "update")

        args = parser.parse_args(["uninstall"])
        self.assertEqual(args.command, "uninstall")

        args = parser.parse_args(["complete-pairing"])
        self.assertEqual(args.command, "complete-pairing")


if __name__ == "__main__":
    unittest.main()
