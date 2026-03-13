from __future__ import annotations

import unittest

from minic.cli import build_parser


class CliTests(unittest.TestCase):
    def test_parser_accepts_commands(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["setup"])
        self.assertEqual(args.command, "setup")

        args = parser.parse_args(["service"])
        self.assertEqual(args.command, "service")

        args = parser.parse_args(["reset-auth"])
        self.assertEqual(args.command, "reset-auth")


if __name__ == "__main__":
    unittest.main()
