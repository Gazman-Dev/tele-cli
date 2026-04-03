from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app_shell import AppShell, DefaultAppShellBackend
from core.json_store import load_json, save_json
from core.models import AuthState, CodexServerState, Config, LockMetadata, RuntimeState, SetupState
from core.paths import build_paths
from demo_ui.state import DemoExit
from integrations.telegram import TelegramError
from storage.runtime_state_store import save_codex_server_state, save_runtime_state


class FakeUi:
    def __init__(
        self,
        keys: list[str],
        inputs: list[str] | None = None,
        timed_keys: list[str | None] | None = None,
    ) -> None:
        self.is_tty = True
        self._keys = list(keys)
        self._inputs = list(inputs or [])
        self._timed_keys = list(timed_keys or [])
        self.begin_called = False
        self.end_called = False
        self.pause_messages: list[str] = []
        self.renders: list[list[str]] = []
        self.spinners: list[tuple[str, float]] = []

    def begin(self) -> None:
        self.begin_called = True

    def end(self) -> None:
        self.end_called = True

    def render(self, lines: list[str]) -> None:
        self.renders.append(lines)

    def read_key(self) -> str:
        if self._keys:
            return self._keys.pop(0)
        return "q"

    def pause(self, message: str = "Press Enter to continue...") -> None:
        self.pause_messages.append(message)

    def input_section(
        self,
        prompt: str,
        panel_width: int,
        typed: str = "",
        title: str = "Input",
    ) -> list[str]:
        return [f"INPUT {title} {prompt}"]

    def input_line(self, prompt: str, panel_width: int = 72, use_existing_field: bool = False) -> str:
        if self._inputs:
            return self._inputs.pop(0)
        return ""

    def timed_keypress(self, delay_seconds: float) -> str | None:
        if self._timed_keys:
            return self._timed_keys.pop(0)
        return None

    def spinner(self, text: str, duration: float = 0.8) -> None:
        self.spinners.append((text, duration))

    def print_header(self) -> list[str]:
        return ["HEADER"]

    def system_strip(
        self,
        service_state: str,
        codex_state: str,
        telegram_state: str,
        summary: str,
    ) -> list[str]:
        return [f"SYSTEM {service_state} {codex_state} {telegram_state} {summary}"]

    def panel(self, title: str, lines: list[str], width: int = 72, align: str = "left") -> list[str]:
        return [f"PANEL {title}"] + list(lines)

    def splash_frame(self, frame_index: int) -> list[str]:
        return [f"SPLASH {frame_index}"]


class InterruptingUi(FakeUi):
    def __init__(self, *, fail_on: str, exception: BaseException, **kwargs) -> None:
        super().__init__(**kwargs)
        self.fail_on = fail_on
        self.exception = exception
        self.failed = False

    def read_key(self) -> str:
        if self.fail_on == "read_key" and not self.failed:
            self.failed = True
            raise self.exception
        return super().read_key()

    def input_line(self, prompt: str, panel_width: int = 72, use_existing_field: bool = False) -> str:
        if self.fail_on == "input_line" and not self.failed:
            self.failed = True
            raise self.exception
        return super().input_line(prompt, panel_width=panel_width, use_existing_field=use_existing_field)


class FakeBackend:
    def __init__(self) -> None:
        self.actions: list[str] = []
        self.setup_choices = []
        self.uninstall_calls = 0
        self.dependency_steps: list[str] = []
        self.dependency_error: str | None = None
        self.validated_tokens: list[str] = []
        self.pairing_polls: list[int | None] = []
        self.pairing_confirmations: list[str] = []
        self.poll_results: list[tuple[int | None, str, str | None]] = []
        self.confirm_results: list[tuple[bool, str | None]] = []
        self.update_calls = 0
        self.update_result: tuple[bool, str | None] = (True, None)
        self.duplicate_registrations: list[str] = []
        self.repaired_duplicates: list[str] = []
        self.duplicate_repair_calls = 0
        self.codex_login_runs = 0

    def build_status(self, paths):
        from app_shell import AppShellStatus

        return AppShellStatus(
            service_state="running",
            codex_state="running",
            telegram_state="paired",
            status_line="ready",
            detail_lines=["detail-a", "detail-b"],
        )

    def build_menu_items(self, paths):
        from demo_ui.state import MenuItem

        return [MenuItem("Run setup", "setup"), MenuItem("Exit", "exit")]

    def ensure_service_running(self, paths) -> str | None:
        self.actions.append("ensure-service")
        return None

    def perform_action(self, paths, action: str) -> str | None:
        self.actions.append(action)
        if action == "login-codex":
            self.codex_login_runs += 1
        if action == "exit":
            return "exit"
        return None

    def perform_setup(self, paths, recovery_choices=None) -> str | None:
        self.actions.append("setup")
        self.setup_choices.append(recovery_choices)
        return None

    def perform_update(self, paths) -> tuple[bool, str | None]:
        self.update_calls += 1
        return self.update_result

    def perform_uninstall(self, paths) -> str | None:
        self.uninstall_calls += 1
        return "exit"

    def build_logs_view(self, paths, view: str, *, limit: int = 20) -> list[str]:
        return [f"log-view={view}", f"limit={limit}"]

    def ensure_dependencies(self, paths) -> tuple[list[str], str | None]:
        return list(self.dependency_steps), self.dependency_error

    def get_duplicate_service_registrations(self, paths) -> list[str]:
        return list(self.duplicate_registrations)

    def repair_duplicate_service_registrations(self, paths) -> list[str]:
        self.duplicate_repair_calls += 1
        self.duplicate_registrations = []
        return list(self.repaired_duplicates)

    def validate_and_save_token(self, paths, token: str) -> tuple[bool, str | None]:
        self.validated_tokens.append(token)
        auth = AuthState(bot_token=token)
        save_json(paths.auth, auth.to_dict())
        return True, None

    def poll_pairing_request(self, paths, offset: int | None) -> tuple[int | None, str, str | None]:
        self.pairing_polls.append(offset)
        if self.poll_results:
            result = self.poll_results.pop(0)
        else:
            result = (offset, "paired", None)
        if result[1] == "code-issued":
            save_json(
                paths.auth,
                AuthState(
                    bot_token="token",
                    pairing_code=result[2],
                    pending_user_id=11,
                    pending_chat_id=22,
                ).to_dict(),
            )
        elif result[1] == "paired":
            save_json(
                paths.auth,
                AuthState(
                    bot_token="token",
                    telegram_user_id=11,
                    telegram_chat_id=22,
                    paired_at="now",
                ).to_dict(),
            )
        return result

    def confirm_pairing(self, paths, code: str) -> tuple[bool, str | None]:
        self.pairing_confirmations.append(code)
        if self.confirm_results:
            result = self.confirm_results.pop(0)
            if result[0]:
                save_json(
                    paths.auth,
                    AuthState(
                        bot_token="token",
                        telegram_user_id=11,
                        telegram_chat_id=22,
                        paired_at="now",
                    ).to_dict(),
                )
            return result
        save_json(
            paths.auth,
            AuthState(
                bot_token="token",
                telegram_user_id=11,
                telegram_chat_id=22,
                paired_at="now",
            ).to_dict(),
        )
        return True, None


class AppShellTests(unittest.TestCase):
    def test_default_backend_pairing_poll_surfaces_send_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            save_json(paths.auth, AuthState(bot_token="token").to_dict())
            backend = DefaultAppShellBackend()

            class FailingTelegram:
                def __init__(self, token: str) -> None:
                    self.token = token

                def get_updates(self, offset=None, timeout: int = 20) -> list[dict]:
                    return [
                        {
                            "update_id": 7,
                            "message": {
                                "chat": {"id": 22},
                                "from": {"id": 11},
                            },
                        }
                    ]

                def send_message(self, chat_id: int, text: str, topic_id: int | None = None, parse_mode=None):
                    raise TelegramError("{'ok': False, 'error_code': 400, 'description': 'Bad Request: chat not found'}")

            with patch("app_shell.TelegramClient", FailingTelegram):
                offset, status, payload = backend.poll_pairing_request(paths, None)

            self.assertEqual(offset, 8)
            self.assertEqual(status, "error")
            self.assertIn("chat not found", payload or "")

    def test_default_backend_pairing_poll_replies_in_message_topic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            save_json(paths.auth, AuthState(bot_token="token").to_dict())
            backend = DefaultAppShellBackend()
            sent: list[tuple[int, str, int | None]] = []

            class TopicTelegram:
                def __init__(self, token: str) -> None:
                    self.token = token

                def get_updates(self, offset=None, timeout: int = 20) -> list[dict]:
                    return [
                        {
                            "update_id": 7,
                            "message": {
                                "chat": {"id": 22},
                                "from": {"id": 11},
                                "message_thread_id": 99,
                            },
                        }
                    ]

                def send_message(self, chat_id: int, text: str, topic_id: int | None = None, parse_mode=None):
                    sent.append((chat_id, text, topic_id))
                    return {"message_id": 1}

            with patch("app_shell.TelegramClient", TopicTelegram):
                offset, status, payload = backend.poll_pairing_request(paths, None)

            self.assertEqual(offset, 8)
            self.assertEqual(status, "code-issued")
            self.assertIsNotNone(payload)
            self.assertEqual(sent, [(22, f"Pairing code: {payload}. Enter this code in the local Tele Cli setup terminal.", 99)])

    def test_default_backend_confirm_pairing_falls_back_when_topic_is_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            save_json(
                paths.auth,
                AuthState(
                    bot_token="token",
                    pairing_code="12345",
                    pending_user_id=11,
                    pending_chat_id=22,
                    pending_topic_id=99,
                ).to_dict(),
            )
            backend = DefaultAppShellBackend()
            sent: list[tuple[int, str, int | None]] = []

            class TopicClosingTelegram:
                def __init__(self, token: str) -> None:
                    self.token = token

                def send_message(self, chat_id: int, text: str, topic_id: int | None = None, parse_mode=None):
                    sent.append((chat_id, text, topic_id))
                    if topic_id is not None:
                        raise TelegramError("{'ok': False, 'error_code': 400, 'description': 'Bad Request: TOPIC_CLOSED'}")
                    return {"message_id": 1}

            with patch("app_shell.TelegramClient", TopicClosingTelegram):
                ok, message = backend.confirm_pairing(paths, "12345")

            self.assertTrue(ok)
            self.assertIsNone(message)
            self.assertEqual(
                sent,
                [
                    (22, "Pairing complete. Tele Cli is now authorized for this chat.", 99),
                    (22, "Pairing complete. Tele Cli is now authorized for this chat.", None),
                ],
            )
            saved = load_json(paths.auth, AuthState.from_dict)
            self.assertIsNotNone(saved)
            self.assertIsNone(saved.telegram_topic_id)
    def test_default_backend_includes_pending_pairing_menu_item(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            save_json(paths.config, Config(state_dir=str(paths.root)).to_dict())
            save_json(
                paths.auth,
                AuthState(
                    bot_token="token",
                    pairing_code="123456",
                    pending_user_id=11,
                    pending_chat_id=22,
                ).to_dict(),
            )

            items = DefaultAppShellBackend().build_menu_items(paths)

            self.assertIn("Complete Telegram pairing", [item.label for item in items])

    def test_default_backend_includes_codex_login_menu_item_when_auth_required(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            save_json(paths.config, Config(state_dir=str(paths.root)).to_dict())
            save_codex_server_state(
                paths,
                CodexServerState(
                    transport="stdio://",
                    initialized=True,
                    auth_required=True,
                    login_type="chatgpt",
                    login_url="https://example.test/login",
                    capabilities={"threads": True},
                ),
            )

            items = DefaultAppShellBackend().build_menu_items(paths)

            self.assertIn("Log In Codex", [item.label for item in items])

    def test_default_backend_includes_view_logs_menu_item(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            save_json(paths.config, Config(state_dir=str(paths.root)).to_dict())

            items = DefaultAppShellBackend().build_menu_items(paths)

            self.assertIn("View logs", [item.label for item in items])

    def test_default_backend_status_uses_runtime_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            save_json(paths.config, Config(state_dir=str(paths.root)).to_dict())
            save_json(
                paths.app_lock,
                LockMetadata(
                    pid=1,
                    hostname="host",
                    username="user",
                    started_at="now",
                    mode="service",
                    timestamp="now",
                    app_version="1",
                    cwd=str(paths.root),
                ).to_dict(),
            )
            save_runtime_state(
                paths,
                RuntimeState(
                    session_id="1",
                    service_state="RUNNING",
                    codex_state="AUTH_REQUIRED",
                    telegram_state="RUNNING",
                    recorder_state="RUNNING",
                    debug_state="IDLE",
                ),
            )

            with patch("app_shell.LockFile.inspect") as inspect:
                inspect.return_value = type(
                    "Inspection",
                    (),
                    {
                        "exists": True,
                        "metadata": LockMetadata(
                            pid=1,
                            hostname="host",
                            username="user",
                            started_at="now",
                            mode="service",
                            timestamp="now",
                            app_version="1",
                            cwd=str(paths.root),
                        ),
                        "live": True,
                    },
                )()
                status = DefaultAppShellBackend().build_status(paths)

            self.assertEqual(status.service_state, "running")
            self.assertEqual(status.codex_state, "auth required")
            self.assertEqual(status.telegram_state, "running")
            self.assertIn("codex=AUTH_REQUIRED", status.status_line)

    def test_default_backend_status_surfaces_codex_login_required(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            save_json(paths.config, Config(state_dir=str(paths.root)).to_dict())
            save_json(
                paths.auth,
                AuthState(
                    bot_token="token",
                    telegram_user_id=11,
                    telegram_chat_id=22,
                    paired_at="now",
                ).to_dict(),
            )
            save_runtime_state(
                paths,
                RuntimeState(
                    session_id="1",
                    service_state="RUNNING",
                    codex_state="AUTH_REQUIRED",
                    telegram_state="RUNNING",
                    recorder_state="RUNNING",
                    debug_state="RUNNING",
                ),
            )
            save_codex_server_state(
                paths,
                CodexServerState(
                    transport="stdio://",
                    initialized=True,
                    account_status="auth_required",
                    auth_required=True,
                    login_type="chatgpt",
                    login_url="https://example.test/login",
                    capabilities={"threads": True},
                ),
            )

            with patch("app_shell.LockFile.inspect") as inspect:
                inspect.return_value = type(
                    "Inspection",
                    (),
                    {
                        "exists": True,
                        "metadata": LockMetadata(
                            pid=1,
                            hostname="host",
                            username="user",
                            started_at="now",
                            mode="service",
                            timestamp="now",
                            app_version="1",
                            cwd=str(paths.root),
                        ),
                        "live": True,
                    },
                )()
                status = DefaultAppShellBackend().build_status(paths)

            self.assertEqual(status.service_state, "running")
            self.assertEqual(status.codex_state, "auth required")
            self.assertEqual(status.telegram_state, "running")
            self.assertEqual(status.status_line, "AI Service (Codex) login required.")
            self.assertIn("Login URL:", "\n".join(status.detail_lines))

    def test_default_backend_status_ignores_stale_runtime_when_service_is_not_live(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            save_json(paths.config, Config(state_dir=str(paths.root)).to_dict())
            save_json(
                paths.auth,
                AuthState(
                    bot_token="token",
                    telegram_user_id=11,
                    telegram_chat_id=22,
                    paired_at="now",
                ).to_dict(),
            )
            save_runtime_state(
                paths,
                RuntimeState(
                    session_id="1",
                    service_state="RUNNING",
                    codex_state="RUNNING",
                    telegram_state="RUNNING",
                    recorder_state="RUNNING",
                    debug_state="RUNNING",
                ),
            )

            status = DefaultAppShellBackend().build_status(paths)

            self.assertEqual(status.service_state, "error")
            self.assertEqual(status.codex_state, "running")
            self.assertEqual(status.telegram_state, "running")
            self.assertEqual(status.status_line, "last known telegram=RUNNING codex=RUNNING")

    def test_default_backend_ensure_service_running_starts_managed_service(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            backend = DefaultAppShellBackend()

            with (
                patch("app_shell.current_service_manager") as current_manager,
                patch("app_shell.ensure_service_registration") as ensure_registration,
            ):
                current_manager.return_value = object()
                ensure_registration.return_value = type(
                    "EnsureResult",
                    (),
                    {
                        "action": "started",
                    },
                )()
                self.assertIsNone(backend.ensure_service_running(paths))

            ensure_registration.assert_called_once()

    def test_shell_runs_startup_action_before_status_loop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            save_json(
                paths.auth,
                AuthState(
                    bot_token="token",
                    telegram_user_id=11,
                    telegram_chat_id=22,
                    paired_at="now",
                ).to_dict(),
            )
            backend = FakeBackend()
            ui = FakeUi(keys=["down", "enter"])

            with patch("app_shell.time.sleep", return_value=None):
                AppShell(paths, backend=backend, ui=ui).run(startup_action="setup")

            self.assertEqual(backend.actions, ["setup", "ensure-service", "ensure-service", "exit"])
            self.assertTrue(ui.begin_called)
            self.assertTrue(ui.end_called)
            self.assertTrue(ui.renders)
            self.assertEqual(ui.pause_messages, [])

    def test_unconfigured_launch_auto_enters_setup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            backend = FakeBackend()
            ui = FakeUi(keys=["down", "enter"], inputs=["bot-token"])

            with patch("app_shell.time.sleep", return_value=None):
                AppShell(paths, backend=backend, ui=ui).run()

            self.assertEqual(backend.validated_tokens, ["bot-token"])
            self.assertEqual(backend.actions, ["setup", "ensure-service", "ensure-service", "exit"])
            rendered_text = "\n".join("\n".join(lines) for lines in ui.renders)
            self.assertIn("Telegram Bot Setup", rendered_text)

    def test_configured_launch_stays_on_status_screen(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            save_json(
                paths.auth,
                AuthState(
                    bot_token="token",
                    telegram_user_id=11,
                    telegram_chat_id=22,
                    paired_at="now",
                ).to_dict(),
            )
            backend = FakeBackend()
            ui = FakeUi(keys=["down", "enter"])

            with patch("app_shell.time.sleep", return_value=None):
                AppShell(paths, backend=backend, ui=ui).run()

            self.assertEqual(backend.actions, ["ensure-service", "exit"])
            rendered_text = "\n".join(ui.renders[-1])
            self.assertIn("PANEL Status", rendered_text)
            self.assertIn("PANEL Menu", rendered_text)

    def test_shell_bootstraps_missing_dependencies_on_startup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            save_json(
                paths.auth,
                AuthState(
                    bot_token="token",
                    telegram_user_id=11,
                    telegram_chat_id=22,
                    paired_at="now",
                ).to_dict(),
            )
            backend = FakeBackend()
            backend.dependency_steps = ["Installing npm via brew", "Installing Codex CLI"]
            ui = FakeUi(keys=["down", "enter"])

            with patch("app_shell.time.sleep", return_value=None):
                AppShell(paths, backend=backend, ui=ui).run()

            self.assertEqual(
                ui.spinners,
                [("Installing npm via brew", 0.45), ("Installing Codex CLI", 0.45)],
            )
            rendered_text = "\n".join("\n".join(lines) for lines in ui.renders)
            self.assertIn("Checking Dependencies", rendered_text)
            self.assertIn("Dependencies are ready.", rendered_text)

    def test_shell_reports_dependency_bootstrap_failure_and_continues(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            save_json(
                paths.auth,
                AuthState(
                    bot_token="token",
                    telegram_user_id=11,
                    telegram_chat_id=22,
                    paired_at="now",
                ).to_dict(),
            )
            backend = FakeBackend()
            backend.dependency_error = "npm install failed"
            ui = FakeUi(keys=["down", "enter"])

            with patch("app_shell.time.sleep", return_value=None):
                AppShell(paths, backend=backend, ui=ui).run()

            self.assertEqual(ui.pause_messages, ["Press Enter to continue to Tele Cli..."])
            rendered_text = "\n".join("\n".join(lines) for lines in ui.renders)
            self.assertIn("Automatic dependency install failed.", rendered_text)
            self.assertIn("npm install failed", rendered_text)

    def test_shell_runs_selected_menu_action_and_pauses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            save_json(
                paths.auth,
                AuthState(
                    bot_token="token",
                    telegram_user_id=11,
                    telegram_chat_id=22,
                    paired_at="now",
                ).to_dict(),
            )
            backend = FakeBackend()
            ui = FakeUi(keys=["enter", "down", "enter"])

            with patch("app_shell.time.sleep", return_value=None):
                AppShell(paths, backend=backend, ui=ui).run()

            self.assertEqual(backend.actions, ["ensure-service", "setup", "ensure-service", "exit"])
            self.assertEqual(ui.pause_messages, ["Press Enter to return to Tele Cli..."])
            rendered_text = "\n".join(ui.renders[-1])
            self.assertIn("PANEL Status", rendered_text)
            self.assertIn("PANEL Menu", rendered_text)

    def test_shell_opens_logs_view_and_returns_to_status_loop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))

            class LogsBackend(FakeBackend):
                def build_menu_items(self, paths):
                    from demo_ui.state import MenuItem

                    return [MenuItem("View logs", "logs"), MenuItem("Exit", "exit")]

            backend = LogsBackend()
            ui = FakeUi(keys=["enter", "q", "down", "enter"])

            with patch("app_shell.time.sleep", return_value=None):
                AppShell(paths, backend=backend, ui=ui).run()

            rendered = ["\n".join(frame) for frame in ui.renders]
            self.assertTrue(any("Logs: Recent Events" in frame for frame in rendered))
            self.assertTrue(any("log-view=recent" in frame for frame in rendered))

    def test_shell_runs_codex_login_action_inside_shell_flow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            save_json(
                paths.auth,
                AuthState(
                    bot_token="token",
                    telegram_user_id=11,
                    telegram_chat_id=22,
                    paired_at="now",
                ).to_dict(),
            )

            class LoginBackend(FakeBackend):
                def build_menu_items(self, paths):
                    from demo_ui.state import MenuItem

                    return [MenuItem("Log In Codex", "login-codex"), MenuItem("Exit", "exit")]

            backend = LoginBackend()
            ui = FakeUi(keys=["enter", "down", "enter"])

            with patch("app_shell.time.sleep", return_value=None):
                AppShell(paths, backend=backend, ui=ui).run()

            self.assertEqual(backend.codex_login_runs, 1)
            self.assertEqual(backend.actions, ["ensure-service", "login-codex", "exit"])
            self.assertTrue(ui.end_called)
            self.assertEqual(ui.pause_messages, ["Press Enter to return to Tele Cli..."])

    def test_shell_setup_action_uses_token_screen_before_backend_setup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            backend = FakeBackend()
            ui = FakeUi(keys=["down", "enter"], inputs=["bot-token"])

            with patch("app_shell.time.sleep", return_value=None):
                AppShell(paths, backend=backend, ui=ui).run(startup_action="setup")

            self.assertEqual(backend.validated_tokens, ["bot-token"])
            self.assertEqual(backend.actions, ["setup", "ensure-service", "ensure-service", "exit"])
            rendered_text = "\n".join("\n".join(lines) for lines in ui.renders)
            self.assertIn("Telegram Bot Setup", rendered_text)

    def test_shell_retries_token_screen_on_validation_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))

            class RetryingBackend(FakeBackend):
                def validate_and_save_token(self, paths, token: str) -> tuple[bool, str | None]:
                    self.validated_tokens.append(token)
                    if len(self.validated_tokens) == 1:
                        return False, "bad token"
                    return True, None

            backend = RetryingBackend()
            ui = FakeUi(keys=["down", "enter"], inputs=["bad", "good"])

            with patch("app_shell.time.sleep", return_value=None):
                AppShell(paths, backend=backend, ui=ui).run(startup_action="setup")

            self.assertEqual(backend.validated_tokens, ["bad", "good"])
            rendered_text = "\n".join("\n".join(lines) for lines in ui.renders)
            self.assertIn("bad token", rendered_text)

    def test_shell_setup_action_uses_pairing_screen_before_backend_setup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            save_json(paths.auth, AuthState(bot_token="token").to_dict())
            backend = FakeBackend()
            backend.poll_results = [(1, "code-issued", "123456")]
            ui = FakeUi(keys=["down", "enter"], inputs=["123456"])

            with patch("app_shell.time.sleep", return_value=None):
                AppShell(paths, backend=backend, ui=ui).run(startup_action="setup")

            self.assertEqual(backend.pairing_polls, [None])
            self.assertEqual(backend.pairing_confirmations, ["123456"])
            self.assertEqual(backend.actions, ["setup", "ensure-service", "ensure-service", "exit"])
            rendered_text = "\n".join("\n".join(lines) for lines in ui.renders)
            self.assertIn("Telegram Pairing", rendered_text)
            self.assertNotIn("123456", rendered_text)
            self.assertIn("Type the Telegram code", rendered_text)

    def test_shell_pairing_screen_retries_bad_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            save_json(paths.auth, AuthState(bot_token="token").to_dict())
            backend = FakeBackend()
            backend.poll_results = [(1, "code-issued", "123456"), (2, "code-issued", "123456")]
            backend.confirm_results = [
                (False, "Invalid pairing code. Enter the current code from Telegram."),
                (True, None),
            ]
            ui = FakeUi(keys=["down", "enter"], inputs=["bad", "123456"])

            with patch("app_shell.time.sleep", return_value=None):
                AppShell(paths, backend=backend, ui=ui).run(startup_action="setup")

            self.assertEqual(backend.pairing_confirmations, ["bad", "123456"])
            rendered_text = "\n".join("\n".join(lines) for lines in ui.renders)
            self.assertIn("Invalid pairing code", rendered_text)

    def test_escape_during_setup_returns_to_main_menu(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            ui = InterruptingUi(
                fail_on="input_line",
                exception=DemoExit(0),
                keys=["down", "enter"],
                inputs=["ignored"],
            )
            backend = FakeBackend()

            with patch("app_shell.time.sleep", return_value=None):
                AppShell(paths, backend=backend, ui=ui).run(startup_action="setup")

            self.assertEqual(backend.actions, ["exit"])
            rendered_text = "\n".join("\n".join(lines) for lines in ui.renders)
            self.assertIn("PANEL Menu", rendered_text)

    def test_control_c_on_status_screen_does_not_exit_app(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            save_json(
                paths.auth,
                AuthState(
                    bot_token="token",
                    telegram_user_id=11,
                    telegram_chat_id=22,
                    paired_at="now",
                ).to_dict(),
            )
            ui = InterruptingUi(
                fail_on="read_key",
                exception=KeyboardInterrupt(),
                keys=["down", "enter"],
            )
            backend = FakeBackend()

            with patch("app_shell.time.sleep", return_value=None):
                AppShell(paths, backend=backend, ui=ui).run()

            self.assertEqual(backend.actions, ["ensure-service", "ensure-service", "exit"])
            rendered_text = "\n".join("\n".join(lines) for lines in ui.renders)
            self.assertIn("PANEL Menu", rendered_text)

    def test_escape_on_status_screen_keeps_shell_open_until_explicit_exit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            save_json(
                paths.auth,
                AuthState(
                    bot_token="token",
                    telegram_user_id=11,
                    telegram_chat_id=22,
                    paired_at="now",
                ).to_dict(),
            )
            backend = FakeBackend()
            ui = FakeUi(keys=["esc", "down", "enter"])

            with patch("app_shell.time.sleep", return_value=None):
                AppShell(paths, backend=backend, ui=ui).run()

            self.assertEqual(backend.actions, ["ensure-service", "exit"])

    def test_shell_runs_update_flow_in_shell(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            backend = FakeBackend()
            ui = FakeUi(keys=["enter", "down", "enter"])

            with patch("app_shell.time.sleep", return_value=None):
                AppShell(paths, backend=backend, ui=ui).run(startup_action="update")

            self.assertEqual(backend.update_calls, 1)
            self.assertNotIn("update", backend.actions)
            self.assertEqual(ui.pause_messages, [])
            rendered_text = "\n".join("\n".join(lines) for lines in ui.renders)
            self.assertIn("Updating Tele Cli", rendered_text)
            self.assertIn("Update complete.", rendered_text)
            self.assertIn("Choose what to do next:", rendered_text)
            self.assertIn("PANEL Next", rendered_text)

    def test_shell_update_flow_can_exit_after_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            backend = FakeBackend()
            ui = FakeUi(keys=["down", "enter"])

            with patch("app_shell.time.sleep", return_value=None):
                AppShell(paths, backend=backend, ui=ui).run(startup_action="update")

            self.assertEqual(backend.update_calls, 1)
            self.assertEqual(backend.actions, [])

    def test_shell_shows_update_failure_in_shell(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            backend = FakeBackend()
            backend.update_result = (False, "repair declined")
            ui = FakeUi(keys=["down", "enter"])

            with patch("app_shell.time.sleep", return_value=None):
                AppShell(paths, backend=backend, ui=ui).run(startup_action="update")

            self.assertEqual(backend.update_calls, 1)
            self.assertEqual(ui.pause_messages, ["Press Enter to return to Tele Cli..."])
            rendered_text = "\n".join("\n".join(lines) for lines in ui.renders)
            self.assertIn("Update failed.", rendered_text)
            self.assertIn("repair declined", rendered_text)

    def test_shell_repairs_duplicate_services_before_update(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            backend = FakeBackend()
            backend.duplicate_registrations = ["tele-cli-copy (launchd)"]
            backend.repaired_duplicates = ["tele-cli-copy (launchd)"]
            ui = FakeUi(keys=["r", "down", "enter"])

            with patch("app_shell.time.sleep", return_value=None):
                AppShell(paths, backend=backend, ui=ui).run(startup_action="update")

            self.assertEqual(backend.duplicate_repair_calls, 1)
            self.assertEqual(backend.update_calls, 1)
            rendered_text = "\n".join("\n".join(lines) for lines in ui.renders)
            self.assertIn("Duplicate Services", rendered_text)
            self.assertIn("Duplicate registrations removed.", rendered_text)

    def test_shell_cancels_update_when_duplicate_repair_is_declined(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            backend = FakeBackend()
            backend.duplicate_registrations = ["tele-cli-copy (launchd)"]
            ui = FakeUi(keys=["c", "down", "enter"])

            with patch("app_shell.time.sleep", return_value=None):
                AppShell(paths, backend=backend, ui=ui).run(startup_action="update")

            self.assertEqual(backend.duplicate_repair_calls, 0)
            self.assertEqual(backend.update_calls, 0)
            self.assertEqual(ui.pause_messages, ["Press Enter to return to Tele Cli..."])
            rendered_text = "\n".join("\n".join(lines) for lines in ui.renders)
            self.assertIn("Update cancelled.", rendered_text)

    def test_shell_repairs_duplicate_services_before_setup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            save_json(
                paths.auth,
                AuthState(
                    bot_token="token",
                    telegram_user_id=11,
                    telegram_chat_id=22,
                    paired_at="now",
                ).to_dict(),
            )
            backend = FakeBackend()
            backend.duplicate_registrations = ["tele-cli-copy (launchd)"]
            backend.repaired_duplicates = ["tele-cli-copy (launchd)"]
            ui = FakeUi(keys=["r", "down", "enter"])

            with patch("app_shell.time.sleep", return_value=None):
                AppShell(paths, backend=backend, ui=ui).run(startup_action="setup")

            self.assertEqual(backend.duplicate_repair_calls, 1)
            self.assertEqual(backend.actions, ["setup", "ensure-service", "ensure-service", "exit"])

    def test_shell_collects_interrupted_setup_resolution_before_running_setup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            save_json(
                paths.setup_lock,
                SetupState(
                    status="failed",
                    pid=0,
                    timestamp="now",
                    npm_installed=True,
                    codex_installed=True,
                    telegram_token_saved=True,
                    telegram_validated=False,
                ).to_dict(),
            )
            save_json(
                paths.auth,
                AuthState(
                    bot_token="token",
                    telegram_user_id=11,
                    telegram_chat_id=22,
                    paired_at="now",
                ).to_dict(),
            )
            backend = FakeBackend()
            ui = FakeUi(keys=["r", "down", "enter"])

            with patch("app_shell.time.sleep", return_value=None):
                AppShell(paths, backend=backend, ui=ui).run(startup_action="setup")

            self.assertEqual(backend.actions, ["setup", "ensure-service", "ensure-service", "exit"])
            self.assertEqual(backend.setup_choices[0].setup_choice, "resume")
            rendered_text = "\n".join("\n".join(lines) for lines in ui.renders)
            self.assertIn("Interrupted Setup", rendered_text)

    def test_shell_collects_lock_resolution_before_running_setup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            save_json(
                paths.app_lock,
                LockMetadata(
                    pid=999999,
                    hostname="host",
                    username="user",
                    started_at="earlier",
                    mode="service",
                    timestamp="now",
                    app_version="1",
                    command=["python", "-m", "cli"],
                    cwd=str(paths.root),
                ).to_dict(),
            )
            save_json(
                paths.auth,
                AuthState(
                    bot_token="token",
                    telegram_user_id=11,
                    telegram_chat_id=22,
                    paired_at="now",
                ).to_dict(),
            )
            backend = FakeBackend()
            ui = FakeUi(keys=["h", "down", "enter"])

            with patch("app_shell.time.sleep", return_value=None):
                AppShell(paths, backend=backend, ui=ui).run(startup_action="setup")

            self.assertEqual(backend.actions, ["setup", "ensure-service", "ensure-service", "exit"])
            self.assertEqual(backend.setup_choices[0].app_lock_choice, "heal")
            rendered_text = "\n".join("\n".join(lines) for lines in ui.renders)
            self.assertIn("Stale App Lock", rendered_text)

    def test_shell_runs_uninstall_inside_shell(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            backend = FakeBackend()
            ui = FakeUi(keys=[], inputs=["uninstall"])

            with patch("app_shell.time.sleep", return_value=None):
                AppShell(paths, backend=backend, ui=ui).run(startup_action="uninstall")

            self.assertEqual(backend.uninstall_calls, 1)
            rendered_text = "\n".join("\n".join(lines) for lines in ui.renders)
            self.assertIn("Uninstall Tele Cli", rendered_text)

    def test_shell_uninstall_cancel_returns_to_status_loop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            backend = FakeBackend()
            ui = FakeUi(keys=["down", "enter"], inputs=["q"])

            with patch("app_shell.time.sleep", return_value=None):
                AppShell(paths, backend=backend, ui=ui).run(startup_action="uninstall")

            self.assertEqual(backend.uninstall_calls, 0)
            rendered_text = "\n".join("\n".join(lines) for lines in ui.renders)
            self.assertIn("PANEL Menu", rendered_text)

    def test_shell_surfaces_service_start_error_without_manual_service_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            save_json(
                paths.auth,
                AuthState(
                    bot_token="token",
                    telegram_user_id=11,
                    telegram_chat_id=22,
                    paired_at="now",
                ).to_dict(),
            )

            class ErrorBackend(FakeBackend):
                def ensure_service_running(self, paths) -> str | None:
                    self.actions.append("ensure-service")
                    return "service start failed"

            backend = ErrorBackend()
            ui = FakeUi(keys=["down", "enter"])

            with patch("app_shell.time.sleep", return_value=None):
                AppShell(paths, backend=backend, ui=ui).run()

            self.assertEqual(backend.actions, ["ensure-service", "exit"])
            rendered_text = "\n".join("\n".join(lines) for lines in ui.renders)
            self.assertIn("AI Service (Codex) failed to start.", rendered_text)
            self.assertIn("service start failed", rendered_text)


if __name__ == "__main__":
    unittest.main()
