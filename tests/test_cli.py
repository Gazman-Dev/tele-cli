from __future__ import annotations

import sys
import unittest
from unittest.mock import patch

from cli import build_parser, main


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

        args = parser.parse_args(["reset-auth"])
        self.assertEqual(args.command, "reset-auth")

        args = parser.parse_args(["update"])
        self.assertEqual(args.command, "update")

        args = parser.parse_args(["uninstall"])
        self.assertEqual(args.command, "uninstall")

        args = parser.parse_args(["complete-pairing"])
        self.assertEqual(args.command, "complete-pairing")

        args = parser.parse_args(["logs", "recent"])
        self.assertEqual(args.command, "logs")
        self.assertEqual(args.logs_target, "recent")

        args = parser.parse_args(["chat"])
        self.assertEqual(args.command, "chat")
        self.assertEqual(args.session_name, "main")

        args = parser.parse_args(["telegram", "session", "message", "--session", "main", "hello"])
        self.assertEqual(args.command, "telegram")
        self.assertEqual(args.telegram_group, "session")
        self.assertEqual(args.telegram_target, "message")
        self.assertEqual(args.session_name, "main")
        self.assertEqual(args.text, "hello")

    def test_parser_accepts_chat_flags(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["-chat", "-session", "my_group/topic1"])

        self.assertTrue(args.chat_mode)
        self.assertEqual(args.session_name, "my_group/topic1")

    def test_main_returns_to_menu_after_setup_in_interactive_terminal(self) -> None:
        with (
            patch("platform.system", return_value="Linux"),
            patch.object(sys, "argv", ["tele-cli", "setup"]),
            patch("cli.run_app_shell") as run_app_shell_mock,
            patch("cli._is_interactive_terminal", return_value=True),
        ):
            main()

        run_app_shell_mock.assert_called_once()
        _, kwargs = run_app_shell_mock.call_args
        self.assertEqual(kwargs["startup_action"], "setup")

    def test_main_returns_to_menu_after_update_in_interactive_terminal(self) -> None:
        with (
            patch("platform.system", return_value="Linux"),
            patch.object(sys, "argv", ["tele-cli", "update"]),
            patch("cli.run_app_shell") as run_app_shell_mock,
            patch("cli._is_interactive_terminal", return_value=True),
        ):
            main()

        run_app_shell_mock.assert_called_once()
        _, kwargs = run_app_shell_mock.call_args
        self.assertEqual(kwargs["startup_action"], "update")

    def test_main_uses_app_shell_for_default_interactive_launch(self) -> None:
        with (
            patch("platform.system", return_value="Linux"),
            patch.object(sys, "argv", ["tele-cli"]),
            patch("cli.run_app_shell") as run_app_shell_mock,
        ):
            main()

        run_app_shell_mock.assert_called_once()
        _, kwargs = run_app_shell_mock.call_args
        self.assertIsNone(kwargs.get("startup_action"))

    def test_main_runs_local_chat_from_quick_flags(self) -> None:
        with (
            patch("platform.system", return_value="Linux"),
            patch.object(sys, "argv", ["tele-cli", "-chat", "-session", "my_group/topic1"]),
            patch("cli.run_local_chat") as run_local_chat_mock,
            patch("cli.run_app_shell") as run_app_shell_mock,
        ):
            main()

        run_local_chat_mock.assert_called_once()
        _, kwargs = run_local_chat_mock.call_args
        self.assertEqual(kwargs["session_name"], "my_group/topic1")
        run_app_shell_mock.assert_not_called()

    def test_main_runs_local_chat_subcommand_with_default_session(self) -> None:
        with (
            patch("platform.system", return_value="Linux"),
            patch.object(sys, "argv", ["tele-cli", "chat"]),
            patch("cli.run_local_chat") as run_local_chat_mock,
        ):
            main()

        run_local_chat_mock.assert_called_once()
        _, kwargs = run_local_chat_mock.call_args
        self.assertEqual(kwargs["session_name"], "main")

    def test_main_runs_setup_directly_without_tty(self) -> None:
        with (
            patch("platform.system", return_value="Linux"),
            patch.object(sys, "argv", ["tele-cli", "setup"]),
            patch("cli.run_setup") as run_setup_mock,
            patch("cli.run_app_shell") as run_app_shell_mock,
            patch("cli._is_interactive_terminal", return_value=False),
        ):
            main()

        run_setup_mock.assert_called_once()
        run_app_shell_mock.assert_not_called()

    def test_main_routes_telegram_session_command(self) -> None:
        with (
            patch("platform.system", return_value="Linux"),
            patch.object(sys, "argv", ["tele-cli", "telegram", "session", "message", "--session", "main", "hello"]),
            patch("cli.run_telegram_command") as run_telegram_command_mock,
        ):
            main()

        run_telegram_command_mock.assert_called_once()

    def test_main_routes_logs_command(self) -> None:
        with (
            patch("platform.system", return_value="Linux"),
            patch.object(sys, "argv", ["tele-cli", "logs", "recent"]),
            patch("cli.run_logs_command") as run_logs_command_mock,
        ):
            main()

        run_logs_command_mock.assert_called_once()

    def test_main_runs_update_directly_without_tty(self) -> None:
        with (
            patch("platform.system", return_value="Linux"),
            patch.object(sys, "argv", ["tele-cli", "update"]),
            patch("cli.run_update") as run_update_mock,
            patch("cli.run_app_shell") as run_app_shell_mock,
            patch("cli._is_interactive_terminal", return_value=False),
        ):
            main()

        run_update_mock.assert_called_once()
        run_app_shell_mock.assert_not_called()

    def test_main_routes_uninstall_to_app_shell_in_interactive_terminal(self) -> None:
        with (
            patch("platform.system", return_value="Linux"),
            patch.object(sys, "argv", ["tele-cli", "uninstall"]),
            patch("cli.run_app_shell") as run_app_shell_mock,
            patch("cli._is_interactive_terminal", return_value=True),
        ):
            main()

        run_app_shell_mock.assert_called_once()
        _, kwargs = run_app_shell_mock.call_args
        self.assertEqual(kwargs["startup_action"], "uninstall")

    def test_main_runs_uninstall_directly_without_tty(self) -> None:
        with (
            patch("platform.system", return_value="Linux"),
            patch.object(sys, "argv", ["tele-cli", "uninstall"]),
            patch("cli.run_uninstall") as run_uninstall_mock,
            patch("cli.run_app_shell") as run_app_shell_mock,
            patch("cli._is_interactive_terminal", return_value=False),
        ):
            main()

        run_uninstall_mock.assert_called_once()
        run_app_shell_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
