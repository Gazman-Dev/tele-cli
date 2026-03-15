from __future__ import annotations

import shutil
import sys
import time
from dataclasses import dataclass
import io
from contextlib import redirect_stdout
from typing import Protocol

from core.json_store import load_json, save_json
from core.logging_utils import append_recovery_log
from core.locks import LockFile
from core.models import AuthState, Config, RuntimeState, SetupState
from core.paths import AppPaths
from core.process import describe_process
from demo_ui.state import Colors, DemoExit, MenuItem
from demo_ui.ui import TerminalUI
from integrations.telegram import (
    TelegramClient,
    confirm_pairing_code,
    describe_pairing,
    has_pending_pairing,
    is_auth_paired,
    register_pairing_request,
)
from runtime.control import ServiceConflict, ServiceConflictChoices, inspect_service_conflict
from runtime.service import reset_auth, run_service
from setup.admin import run_uninstall, run_update
from setup.host_service import build_service_registration, current_service_manager
from setup.recovery import (
    AppLockConflict,
    ExistingSetupConflict,
    SetupRecoveryChoices,
    inspect_existing_app_lock,
    inspect_existing_setup,
)
from setup.service_manager import analyze_service_registrations, repair_duplicate_registrations
from setup.setup_flow import complete_pending_pairing, ensure_local_dependencies, run_setup


@dataclass
class AppShellStatus:
    service_state: str
    codex_state: str
    telegram_state: str
    status_line: str
    detail_lines: list[str]


class AppShellUi(Protocol):
    is_tty: bool

    def begin(self) -> None: ...

    def end(self) -> None: ...

    def render(self, lines: list[str]) -> None: ...

    def read_key(self) -> str: ...

    def pause(self, message: str = "Press Enter to continue...") -> None: ...

    def input_section(
        self,
        prompt: str,
        panel_width: int,
        typed: str = "",
        title: str = "Input",
    ) -> list[str]: ...

    def input_line(self, prompt: str, panel_width: int = 72, use_existing_field: bool = False) -> str: ...

    def timed_keypress(self, delay_seconds: float) -> str | None: ...

    def spinner(self, text: str, duration: float = 0.8) -> None: ...

    def print_header(self) -> list[str]: ...

    def system_strip(
        self,
        service_state: str,
        codex_state: str,
        telegram_state: str,
        summary: str,
    ) -> list[str]: ...

    def panel(self, title: str, lines: list[str], width: int = 72, align: str = "left") -> list[str]: ...

    def splash_frame(self, frame_index: int) -> list[str]: ...


class AppShellBackend(Protocol):
    def build_status(self, paths: AppPaths) -> AppShellStatus: ...

    def build_menu_items(self, paths: AppPaths) -> list[MenuItem]: ...

    def perform_action(self, paths: AppPaths, action: str) -> str | None: ...

    def perform_setup(self, paths: AppPaths, recovery_choices: SetupRecoveryChoices | None = None) -> str | None: ...

    def perform_service_action(
        self,
        paths: AppPaths,
        conflict_choices: ServiceConflictChoices | None = None,
    ) -> str | None: ...

    def perform_update(self, paths: AppPaths) -> tuple[bool, str | None]: ...

    def perform_uninstall(self, paths: AppPaths) -> str | None: ...

    def ensure_dependencies(self, paths: AppPaths) -> tuple[list[str], str | None]: ...

    def validate_and_save_token(self, paths: AppPaths, token: str) -> tuple[bool, str | None]: ...

    def poll_pairing_request(
        self,
        paths: AppPaths,
        offset: int | None,
    ) -> tuple[int | None, str, str | None]: ...

    def confirm_pairing(
        self,
        paths: AppPaths,
        code: str,
    ) -> tuple[bool, str | None]: ...

    def get_duplicate_service_registrations(self, paths: AppPaths) -> list[str]: ...

    def repair_duplicate_service_registrations(self, paths: AppPaths) -> list[str]: ...


class DefaultAppShellBackend:
    def build_status(self, paths: AppPaths) -> AppShellStatus:
        setup = load_json(paths.setup_lock, SetupState.from_dict)
        auth = load_json(paths.auth, AuthState.from_dict)
        runtime = load_json(paths.runtime, RuntimeState.from_dict)
        config = load_json(paths.config, Config.from_dict)
        inspection = LockFile(paths.app_lock).inspect()

        service_state = "stopped"
        if inspection.exists and inspection.metadata:
            if inspection.live:
                service_state = "running"
            else:
                service_state = "blocked"

        codex_state = "not authenticated"
        telegram_state = "not paired"
        status_line = "Configuration required"
        if runtime:
            service_state = runtime.service_state.lower()
            codex_state = runtime.codex_state.lower().replace("_", " ")
            telegram_state = runtime.telegram_state.lower().replace("_", " ")
            status_line = f"service={runtime.service_state} telegram={runtime.telegram_state} codex={runtime.codex_state}"
        elif setup:
            status_line = setup.status

        npm_status = "installed" if shutil.which("npm") else "missing"
        codex_install_status = "installed" if shutil.which("codex") else "missing"
        token_status = "saved" if auth and auth.bot_token else "missing"
        state_dir = config.state_dir if config else str(paths.root)

        detail_lines = [
            f"{Colors.muted}State dir:{Colors.reset} {state_dir}",
            f"{Colors.muted}Setup:{Colors.reset} {setup.status if setup else 'not started'}",
            f"{Colors.muted}npm:{Colors.reset} {npm_status}",
            f"{Colors.muted}Codex CLI:{Colors.reset} {codex_install_status}",
            f"{Colors.muted}Telegram token:{Colors.reset} {token_status}",
            f"{Colors.muted}Telegram pairing:{Colors.reset} {describe_pairing(auth)}",
        ]
        if inspection.exists and inspection.metadata:
            lock_line = f"pid={inspection.metadata.pid} mode={inspection.metadata.mode}"
            if inspection.live:
                detail_lines.append(f"{Colors.muted}Active lock:{Colors.reset} {lock_line}")
            else:
                detail_lines.append(f"{Colors.yellow}Stale lock:{Colors.reset} {lock_line}")

        return AppShellStatus(
            service_state=service_state,
            codex_state=codex_state,
            telegram_state=telegram_state,
            status_line=status_line,
            detail_lines=detail_lines,
        )

    def build_menu_items(self, paths: AppPaths) -> list[MenuItem]:
        items = [
            MenuItem("Status refresh", "refresh"),
            MenuItem("Run setup", "setup"),
            MenuItem("Start service", "service"),
        ]
        auth = load_json(paths.auth, AuthState.from_dict)
        if auth and has_pending_pairing(auth):
            items.append(MenuItem("Complete Telegram pairing", "complete-pairing"))
        items.extend(
            [
                MenuItem("Reset Telegram auth", "reset-auth"),
                MenuItem("Update install", "update"),
                MenuItem("Uninstall", "uninstall"),
                MenuItem("Exit", "exit"),
            ]
        )
        return items

    def perform_action(self, paths: AppPaths, action: str) -> str | None:
        print("\033[2J\033[H", end="")
        if action == "refresh":
            return None
        if action == "complete-pairing":
            auth = load_json(paths.auth, AuthState.from_dict)
            if not auth or not auth.bot_token:
                print("Telegram bot token is not configured.")
            else:
                completed = complete_pending_pairing(paths, auth, TelegramClient(auth.bot_token), allow_empty=True)
                if not completed:
                    print("No pending pairing was completed.")
            return None
        if action == "reset-auth":
            reset_auth(paths)
            print("Telegram auth reset.")
            return None
        if action == "exit":
            return "exit"
        return None

    def perform_setup(self, paths: AppPaths, recovery_choices: SetupRecoveryChoices | None = None) -> str | None:
        run_setup(paths, recovery_choices=recovery_choices)
        return None

    def perform_service_action(
        self,
        paths: AppPaths,
        conflict_choices: ServiceConflictChoices | None = None,
    ) -> str | None:
        run_service(paths, conflict_choices=conflict_choices)
        return None

    def perform_update(self, paths: AppPaths) -> tuple[bool, str | None]:
        with io.StringIO() as buffer, redirect_stdout(buffer):
            try:
                run_update(paths)
            except SystemExit as exc:
                return False, str(exc)
            except Exception as exc:
                return False, str(exc)
        return True, None

    def perform_uninstall(self, paths: AppPaths) -> str | None:
        run_uninstall(paths, require_confirmation=False)
        return "exit"

    def ensure_dependencies(self, paths: AppPaths) -> tuple[list[str], str | None]:
        try:
            return ensure_local_dependencies(paths), None
        except Exception as exc:
            return [], str(exc)

    def validate_and_save_token(self, paths: AppPaths, token: str) -> tuple[bool, str | None]:
        candidate = token.strip()
        if not candidate:
            return False, "Telegram bot token is required."

        auth = load_json(paths.auth, AuthState.from_dict) or AuthState(bot_token=candidate)
        auth.bot_token = candidate
        bot = TelegramClient(candidate)
        try:
            bot.validate()
        except Exception as exc:
            return False, str(exc)
        save_json(paths.auth, auth.to_dict())
        return True, None

    def poll_pairing_request(
        self,
        paths: AppPaths,
        offset: int | None,
    ) -> tuple[int | None, str, str | None]:
        auth = load_json(paths.auth, AuthState.from_dict)
        if not auth or not auth.bot_token:
            return offset, "error", "Telegram bot token is not configured."
        if is_auth_paired(auth):
            return offset, "paired", None

        bot = TelegramClient(auth.bot_token)
        updates = bot.get_updates(offset=offset, timeout=1)
        next_offset = offset
        for update in updates:
            next_offset = update["update_id"] + 1
            ok, status = register_pairing_request(auth, update)
            save_json(paths.auth, auth.to_dict())
            if status == "already-paired":
                chat_id = update.get("message", {}).get("chat", {}).get("id")
                if chat_id:
                    bot.send_message(chat_id, "This bot is already paired to another chat.")
                continue
            if status == "authorized":
                return next_offset, "paired", None
            if status == "code-issued" and auth.pending_chat_id and auth.pairing_code:
                bot.send_message(
                    auth.pending_chat_id,
                    f"Pairing code: {auth.pairing_code}. Enter this code in the local Tele Cli setup terminal.",
                )
                return next_offset, "code-issued", auth.pairing_code
            if ok:
                return next_offset, "paired", None
        return next_offset, "waiting", None

    def confirm_pairing(
        self,
        paths: AppPaths,
        code: str,
    ) -> tuple[bool, str | None]:
        auth = load_json(paths.auth, AuthState.from_dict)
        if not auth or not auth.bot_token:
            return False, "Telegram bot token is not configured."
        if not has_pending_pairing(auth):
            return False, "No pending Telegram pairing request was found."
        if not confirm_pairing_code(auth, code):
            return False, "Invalid pairing code. Enter the current code from Telegram."

        save_json(paths.auth, auth.to_dict())
        bot = TelegramClient(auth.bot_token)
        assert auth.telegram_chat_id is not None
        bot.send_message(auth.telegram_chat_id, "Pairing complete. Tele Cli is now authorized for this chat.")
        append_recovery_log(
            paths.recovery_log,
            f"telegram paired chat_id={auth.telegram_chat_id} user_id={auth.telegram_user_id}",
        )
        return True, None

    def get_duplicate_service_registrations(self, paths: AppPaths) -> list[str]:
        manager = current_service_manager()
        desired = build_service_registration(paths)
        analysis = analyze_service_registrations(
            manager.list_registrations(),
            desired.service_name,
            desired.state_dir,
        )
        return [f"{registration.service_name} ({registration.manager})" for registration in analysis.duplicates]

    def repair_duplicate_service_registrations(self, paths: AppPaths) -> list[str]:
        manager = current_service_manager()
        desired = build_service_registration(paths)
        repaired = repair_duplicate_registrations(manager, desired.service_name, desired.state_dir)
        return [f"{registration.service_name} ({registration.manager})" for registration in repaired.removed]


class AppShell:
    def __init__(
        self,
        paths: AppPaths,
        backend: AppShellBackend | None = None,
        ui: AppShellUi | None = None,
    ) -> None:
        self.paths = paths
        self.backend = backend or DefaultAppShellBackend()
        self.ui = ui or TerminalUI()
        self.selection = 0

    def run(self, startup_action: str | None = None) -> None:
        if not self.ui.is_tty:
            raise SystemExit("Interactive app shell requires a TTY.")

        self.ui.begin()
        try:
            self._show_startup_splash()
            self._bootstrap_dependencies()
            pending_action = startup_action
            pending_initial_setup = startup_action is None and self._needs_initial_setup()
            while True:
                try:
                    if pending_initial_setup:
                        pending_initial_setup = False
                        result = self._run_action("setup", pause=False)
                        if result == "exit":
                            return
                    if pending_action:
                        action = pending_action
                        pending_action = None
                        result = self._run_action(action, pause=False)
                        if result == "exit":
                            return
                    self._status_loop()
                    return
                except (DemoExit, KeyboardInterrupt):
                    self.selection = 0
                    pending_action = None
                    pending_initial_setup = False
        finally:
            self.ui.end()

    def _show_startup_splash(self) -> None:
        for frame in range(19):
            self.ui.render(self.ui.splash_frame(frame))
            time.sleep(0.08)

    def _bootstrap_dependencies(self) -> None:
        steps, error = self.backend.ensure_dependencies(self.paths)
        if not steps and not error:
            return
        if steps:
            self.ui.render(
                self.ui.print_header()
                + self.ui.panel(
                    "Checking Dependencies",
                    [
                        "Tele Cli is preparing required local tools before continuing.",
                        "",
                        f"{Colors.muted}Missing dependencies will be installed automatically.{Colors.reset}",
                    ],
                    width=76,
                    align="center",
                )
            )
            for step in steps:
                self.ui.spinner(step, 0.45)
            self.ui.render(
                self.ui.print_header()
                + self.ui.panel(
                    "Checking Dependencies",
                    [f"{Colors.green}{Colors.bold}Dependencies are ready.{Colors.reset}"],
                    width=76,
                    align="center",
                )
            )
            time.sleep(0.5)
        if error:
            self.ui.render(
                self.ui.print_header()
                + self.ui.panel(
                    "Checking Dependencies",
                    [
                        f"{Colors.red}{Colors.bold}Automatic dependency install failed.{Colors.reset}",
                        "",
                        error,
                    ],
                    width=76,
                    align="center",
                )
            )
            self.ui.pause("Press Enter to continue to Tele Cli...")

    def _needs_token_setup(self) -> bool:
        auth = load_json(self.paths.auth, AuthState.from_dict)
        return not bool(auth and auth.bot_token)

    def _needs_pairing_setup(self) -> bool:
        auth = load_json(self.paths.auth, AuthState.from_dict)
        return not bool(auth and is_auth_paired(auth))

    def _needs_initial_setup(self) -> bool:
        return self._needs_token_setup() or self._needs_pairing_setup()

    def _status_loop(self) -> None:
        while True:
            items = self.backend.build_menu_items(self.paths)
            self.selection %= len(items)
            self._render_status_screen(items)
            key = self.ui.read_key()
            if key == "up":
                self.selection = (self.selection - 1) % len(items)
            elif key == "down":
                self.selection = (self.selection + 1) % len(items)
            elif key == "enter":
                result = self._run_action(items[self.selection].action)
                if result == "exit":
                    return
            elif key in {"q", "esc"}:
                self.selection = 0

    def _render_status_screen(self, items: list[MenuItem]) -> None:
        status = self.backend.build_status(self.paths)
        menu_lines: list[str] = []
        for index, item in enumerate(items):
            prefix = ">" if index == self.selection else " "
            if index == self.selection:
                menu_lines.append(f"{Colors.chip_focus} {prefix} {item.label.ljust(28)} {Colors.reset}")
            else:
                menu_lines.append(f"  {item.label.ljust(29)}")

        lines = (
            self.ui.print_header()
            + self.ui.system_strip(
                status.service_state,
                status.codex_state,
                status.telegram_state,
                status.status_line,
            )
            + [""]
            + self.ui.panel("Status", status.detail_lines, width=76)
            + [""]
            + self.ui.panel("Menu", menu_lines, width=54)
        )
        self.ui.render(lines)

    def _run_action(self, action: str, pause: bool = True) -> str | None:
        recovery_choices: SetupRecoveryChoices | None = None
        if action == "setup":
            recovery_choices = self._resolve_setup_recovery()
            if recovery_choices == "cancel":
                return "cancel"
        if action == "setup" and self._needs_token_setup():
            result = self._run_token_setup_flow()
            if result in {"exit", "cancel"}:
                return result
        if action == "setup" and self._needs_pairing_setup():
            result = self._run_pairing_setup_flow()
            if result in {"exit", "cancel"}:
                return result
        if action in {"setup", "update"}:
            result = self._resolve_duplicate_service_conflicts(action)
            if result is not None:
                if result == "handled":
                    return None
                return result
        if action == "setup":
            result = self.backend.perform_setup(self.paths, recovery_choices=recovery_choices)
            if result != "exit" and pause:
                self.ui.pause("Press Enter to return to Tele Cli...")
            return result
        if action == "service":
            conflict_choices = self._resolve_service_runtime_conflict()
            if conflict_choices == "cancel":
                return "cancel"
            result = self.backend.perform_service_action(self.paths, conflict_choices=conflict_choices)
            if result != "exit" and pause:
                self.ui.pause("Press Enter to return to Tele Cli...")
            return result
        if action == "update":
            return self._run_update_flow()
        if action == "uninstall":
            return self._run_uninstall_flow()
        result = self.backend.perform_action(self.paths, action)
        if result != "exit" and pause:
            self.ui.pause("Press Enter to return to Tele Cli...")
        return result

    def _resolve_setup_recovery(self) -> SetupRecoveryChoices | str | None:
        choices = SetupRecoveryChoices()
        lock_conflict = inspect_existing_app_lock(LockFile(self.paths.app_lock))
        if lock_conflict:
            lock_choice = self._show_app_lock_conflict(lock_conflict)
            if lock_choice == "cancel":
                return "cancel"
            choices.app_lock_choice = lock_choice

        setup_conflict = inspect_existing_setup(self.paths)
        if setup_conflict:
            setup_choice = self._show_setup_conflict(setup_conflict)
            if setup_choice == "cancel":
                return "cancel"
            choices.setup_choice = setup_choice

        if choices.app_lock_choice is None and choices.setup_choice is None:
            return None
        return choices

    def _show_app_lock_conflict(self, conflict: AppLockConflict) -> str:
        while True:
            if conflict.kind == "live":
                title = "Live App Conflict"
                lines = [
                    "Another Tele Cli process appears to be active.",
                    "",
                    describe_process(conflict.metadata),
                    "",
                    f"{Colors.muted}Press k to stop it and continue setup.{Colors.reset}",
                    f"{Colors.muted}Press i to continue without killing it.{Colors.reset}",
                    f"{Colors.muted}Press e to cancel and return to the shell.{Colors.reset}",
                ]
            else:
                title = "Stale App Lock"
                lines = [
                    "A stale Tele Cli lock was found before setup started.",
                    "",
                    describe_process(conflict.metadata),
                    "",
                    f"{Colors.muted}Press h to clear the stale lock and continue.{Colors.reset}",
                    f"{Colors.muted}Press i to continue without clearing it.{Colors.reset}",
                    f"{Colors.muted}Press e to cancel and return to the shell.{Colors.reset}",
                ]
            self.ui.render(self.ui.print_header() + self.ui.panel(title, lines, width=76))
            key = self.ui.read_key()
            if conflict.kind == "live" and key == "k":
                return "kill"
            if conflict.kind == "stale" and key == "h":
                return "heal"
            if key == "i":
                return "ignore"
            if key in {"e", "q", "esc"}:
                return "cancel"

    def _show_setup_conflict(self, conflict: ExistingSetupConflict) -> str:
        while True:
            if conflict.kind == "active":
                title = "Active Setup"
                lines = [
                    "Tele Cli found another setup run marked as active.",
                    "",
                    f"pid={conflict.state.pid}",
                    "",
                    f"{Colors.muted}Press k to stop that setup and continue here.{Colors.reset}",
                    f"{Colors.muted}Press i to continue here without killing it.{Colors.reset}",
                    f"{Colors.muted}Press e to cancel and return to the shell.{Colors.reset}",
                ]
            else:
                title = "Interrupted Setup"
                lines = [
                    "A previous setup did not finish.",
                    "",
                    f"npm_installed={conflict.state.npm_installed}",
                    f"codex_installed={conflict.state.codex_installed}",
                    f"telegram_token_saved={conflict.state.telegram_token_saved}",
                    f"telegram_validated={conflict.state.telegram_validated}",
                    "",
                    f"{Colors.muted}Press r to resume from the saved progress.{Colors.reset}",
                    f"{Colors.muted}Press n to restart setup from the beginning.{Colors.reset}",
                    f"{Colors.muted}Press i to keep the saved state and continue.{Colors.reset}",
                    f"{Colors.muted}Press e to cancel and return to the shell.{Colors.reset}",
                ]
            self.ui.render(self.ui.print_header() + self.ui.panel(title, lines, width=76))
            key = self.ui.read_key()
            if conflict.kind == "active" and key == "k":
                return "kill"
            if conflict.kind == "interrupted" and key == "r":
                return "resume"
            if conflict.kind == "interrupted" and key == "n":
                return "restart"
            if key == "i":
                return "ignore"
            if key in {"e", "q", "esc"}:
                return "cancel"

    def _resolve_service_runtime_conflict(self) -> ServiceConflictChoices | str | None:
        conflict = inspect_service_conflict(LockFile(self.paths.app_lock))
        if conflict is None:
            return None
        choices = ServiceConflictChoices()
        selected = self._show_service_conflict(conflict)
        if selected == "cancel":
            return "cancel"
        choices.conflict_choice = selected
        if conflict.kind == "stale" and selected == "heal" and conflict.orphan_codex_active:
            orphan = self._show_orphan_codex_conflict(conflict)
            if orphan == "cancel":
                return "cancel"
            choices.orphan_choice = orphan
        return choices

    def _show_service_conflict(self, conflict: ServiceConflict) -> str:
        while True:
            if conflict.kind == "live_same_app":
                title = "Live Service Conflict"
                lines = [
                    "Another Tele Cli runtime appears to be active.",
                    "",
                    describe_process(conflict.metadata),
                    "",
                    f"{Colors.muted}Press k to stop it and continue.{Colors.reset}",
                    f"{Colors.muted}Press i to leave it running and continue anyway.{Colors.reset}",
                    f"{Colors.muted}Press e to cancel and return to the shell.{Colors.reset}",
                ]
            elif conflict.kind == "live_unknown":
                title = "Unknown Runtime Owner"
                lines = [
                    "A live process owns the Tele Cli runtime lock, but ownership is unclear.",
                    "",
                    describe_process(conflict.metadata),
                    "",
                    f"{Colors.muted}Press i to ignore the conflict and continue.{Colors.reset}",
                    f"{Colors.muted}Press e to cancel and return to the shell.{Colors.reset}",
                ]
            else:
                title = "Stale Runtime Lock"
                lines = [
                    "A stale Tele Cli runtime lock was found.",
                    "",
                    describe_process(conflict.metadata),
                    "",
                    f"{Colors.muted}Press h to clear it and continue.{Colors.reset}",
                    f"{Colors.muted}Press i to ignore it and continue.{Colors.reset}",
                    f"{Colors.muted}Press e to cancel and return to the shell.{Colors.reset}",
                ]
            self.ui.render(self.ui.print_header() + self.ui.panel(title, lines, width=76))
            key = self.ui.read_key()
            if conflict.kind == "live_same_app" and key == "k":
                return "kill"
            if conflict.kind == "live_unknown" and key == "i":
                return "ignore"
            if conflict.kind == "stale" and key == "h":
                return "heal"
            if key == "i":
                return "ignore"
            if key in {"e", "q", "esc"}:
                return "cancel"

    def _show_orphan_codex_conflict(self, conflict: ServiceConflict) -> str:
        while True:
            lines = [
                "A Codex child process from a previous run may still be active.",
                "",
                f"child_codex_pid={conflict.metadata.child_codex_pid}",
                "",
                f"{Colors.muted}Press k to stop the orphaned Codex process.{Colors.reset}",
                f"{Colors.muted}Press i to leave it alone and continue.{Colors.reset}",
                f"{Colors.muted}Press e to cancel and return to the shell.{Colors.reset}",
            ]
            self.ui.render(self.ui.print_header() + self.ui.panel("Orphaned Codex", lines, width=76))
            key = self.ui.read_key()
            if key == "k":
                return "kill"
            if key == "i":
                return "ignore"
            if key in {"e", "q", "esc"}:
                return "cancel"

    def _run_token_setup_flow(self) -> str | None:
        error = ""
        while True:
            lines = [
                "Connect Telegram to this machine.",
                "",
                "Create a bot with BotFather and paste the token here.",
                "",
                f"{Colors.muted}BotFather: https://t.me/BotFather{Colors.reset}",
            ]
            if error:
                lines.extend(["", f"{Colors.red}{error}{Colors.reset}"])

            self.ui.render(
                self.ui.print_header()
                + self.ui.panel("Telegram Bot Setup", lines, width=74)
                + self.ui.input_section("Paste bot token", 74, title="Bot Token")
            )
            token = self.ui.input_line("Paste bot token", panel_width=74, use_existing_field=True)
            if token.lower() in {"q", "quit"}:
                return "cancel"

            ok, message = self.backend.validate_and_save_token(self.paths, token)
            if ok:
                self.ui.render(
                    self.ui.print_header()
                    + self.ui.panel(
                        "Telegram Bot Setup",
                        [f"{Colors.green}{Colors.bold}Token saved and validated.{Colors.reset}"],
                        align="center",
                    )
                )
                time.sleep(0.9)
                return None
            error = message or "Telegram token validation failed."

    def _run_pairing_setup_flow(self) -> str | None:
        error = ""
        offset: int | None = None
        while True:
            offset, status, payload = self.backend.poll_pairing_request(self.paths, offset)
            if status == "paired":
                return None
            if status == "error":
                error = payload or "Telegram pairing could not start."
            elif status == "code-issued" and not error:
                error = ""

            auth = load_json(self.paths.auth, AuthState.from_dict)
            pending_pairing = bool(auth and has_pending_pairing(auth))
            lines = [
                "Pair this machine with your Telegram user.",
                "",
                "1. Send any message to your bot.",
                "2. The bot replies in Telegram with your unique code.",
                "3. Enter that code on this machine as soon as it arrives.",
            ]
            lines.extend(
                [
                    "",
                    (
                        f"{Colors.green}Pairing request detected. Enter the code from Telegram now.{Colors.reset}"
                        if pending_pairing
                        else f"{Colors.muted}Waiting for the first Telegram message.{Colors.reset}"
                    ),
                ]
            )
            lines.extend(
                [
                    "",
                    f"{Colors.muted}Press Enter with no code to refresh, or q to cancel back to Tele Cli.{Colors.reset}",
                ]
            )
            if error:
                lines.extend(["", f"{Colors.red}{error}{Colors.reset}"])

            self.ui.render(
                self.ui.print_header()
                + self.ui.panel("Telegram Pairing", lines, width=74, align="center")
                + self.ui.input_section("Type the Telegram code", 74, title="Pairing Code")
            )
            entered_code = self.ui.input_line("Type the Telegram code", panel_width=74, use_existing_field=True)
            if entered_code.lower() in {"q", "quit"}:
                return "cancel"
            if not entered_code.strip():
                error = ""
                continue

            ok, message = self.backend.confirm_pairing(self.paths, entered_code)
            if ok:
                self.ui.render(
                    self.ui.print_header()
                    + self.ui.panel(
                        "Telegram Pairing",
                        [f"{Colors.green}{Colors.bold}Device successfully paired.{Colors.reset}"],
                        align="center",
                    )
                )
                time.sleep(0.9)
                return None
            error = message or "Invalid pairing code."

    def _run_update_flow(self) -> str | None:
        self.ui.render(
            self.ui.print_header()
            + self.ui.panel(
                "Updating Tele Cli",
                [
                    "Preparing package refresh and service restart.",
                    "",
                    f"{Colors.muted}The interactive shell will stay open and return to status when finished.{Colors.reset}",
                ],
                width=76,
                align="center",
            )
        )
        self.ui.spinner("Updating Tele Cli", 0.5)
        ok, message = self.backend.perform_update(self.paths)
        if ok:
            self.ui.render(
                self.ui.print_header()
                + self.ui.panel(
                    "Updating Tele Cli",
                    [f"{Colors.green}{Colors.bold}Update complete.{Colors.reset}"],
                    width=76,
                    align="center",
                )
            )
            time.sleep(0.6)
            return None

        self.ui.render(
            self.ui.print_header()
            + self.ui.panel(
                "Updating Tele Cli",
                [
                    f"{Colors.red}{Colors.bold}Update failed.{Colors.reset}",
                    "",
                    message or "Managed update could not complete.",
                ],
                width=76,
                align="center",
            )
        )
        self.ui.pause("Press Enter to return to Tele Cli...")
        return None

    def _run_uninstall_flow(self) -> str | None:
        error = ""
        while True:
            lines = [
                "Remove Tele Cli from this machine.",
                "",
                "Type uninstall to confirm removal.",
                "",
                f"{Colors.muted}Press q to cancel and return to the main status screen.{Colors.reset}",
            ]
            if error:
                lines.extend(["", f"{Colors.red}{error}{Colors.reset}"])
            self.ui.render(
                self.ui.print_header()
                + self.ui.panel("Uninstall Tele Cli", lines, width=74, align="center")
                + self.ui.input_section("Type uninstall", 74, title="Confirmation")
            )
            confirmation = self.ui.input_line("Type uninstall", panel_width=74, use_existing_field=True)
            if confirmation.lower() in {"q", "quit"}:
                return None
            if confirmation.strip() != "uninstall":
                error = "Type uninstall exactly to confirm removal."
                continue
            return self.backend.perform_uninstall(self.paths)

    def _resolve_duplicate_service_conflicts(self, context: str) -> str | None:
        duplicates = self.backend.get_duplicate_service_registrations(self.paths)
        if not duplicates:
            return None
        while True:
            lines = [
                "Tele Cli detected duplicate service registrations for this state directory.",
                "",
                *duplicates,
                "",
                f"{Colors.muted}Press r to remove the extra registrations and continue.{Colors.reset}",
                f"{Colors.muted}Press c to cancel and return to the shell.{Colors.reset}",
            ]
            self.ui.render(
                self.ui.print_header()
                + self.ui.panel("Duplicate Services", lines, width=76)
            )
            key = self.ui.read_key()
            if key == "r":
                removed = self.backend.repair_duplicate_service_registrations(self.paths)
                detail = ", ".join(removed) if removed else "No duplicate registrations were removed."
                self.ui.render(
                    self.ui.print_header()
                    + self.ui.panel(
                        "Duplicate Services",
                        [
                            f"{Colors.green}{Colors.bold}Duplicate registrations removed.{Colors.reset}",
                            "",
                            detail,
                        ],
                        width=76,
                        align="center",
                    )
                )
                time.sleep(0.6)
                return None
            if key in {"c", "q", "esc"}:
                if context == "update":
                    self.ui.render(
                        self.ui.print_header()
                        + self.ui.panel(
                            "Updating Tele Cli",
                            [
                                f"{Colors.yellow}{Colors.bold}Update cancelled.{Colors.reset}",
                                "",
                                "Duplicate service registrations were not repaired.",
                            ],
                            width=76,
                            align="center",
                        )
                    )
                    self.ui.pause("Press Enter to return to Tele Cli...")
                    return "handled"
                return None


def run_app_shell(paths: AppPaths, startup_action: str | None = None) -> None:
    AppShell(paths).run(startup_action=startup_action)
