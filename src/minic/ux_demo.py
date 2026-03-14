from __future__ import annotations

import random
import re
import shutil
import sys
import time
from dataclasses import dataclass
from typing import Optional

from . import APP_VERSION


class Colors:
    reset = "\033[0m"
    bold = "\033[1m"
    dim = "\033[2m"

    text = "\033[38;2;209;213;219m"
    muted = "\033[38;2;107;114;128m"
    blue = "\033[38;2;96;165;250m"
    green = "\033[38;2;74;222;128m"
    green_dim = "\033[38;2;34;197;94m"
    yellow = "\033[38;2;250;204;21m"
    red = "\033[38;2;248;113;113m"
    cyan = "\033[38;2;94;234;212m"

    chip_on = "\033[48;2;22;101;52m\033[38;2;220;252;231m"
    chip_warn = "\033[48;2;133;77;14m\033[38;2;254;249;195m"
    chip_off = "\033[48;2;127;29;29m\033[38;2;254;226;226m"
    chip_focus = "\033[48;2;21;128;61m\033[38;2;240;253;244m"
    border = "\033[38;2;75;85;99m"


ANSI_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


class DemoExit(SystemExit):
    pass


def visible_len(text: str) -> int:
    return len(ANSI_RE.sub("", text))


def terminal_size() -> tuple[int, int]:
    size = shutil.get_terminal_size((100, 30))
    return size.columns, size.lines


@dataclass
class DemoState:
    configured: bool = False
    service_state: str = "stopped"
    codex_state: str = "not authenticated"
    telegram_state: str = "not paired"
    status_line: str = "Configuration required"
    token: str = ""
    pairing_code: str = ""
    pairing_requested: bool = False
    running: bool = True


@dataclass
class MenuItem:
    label: str
    action: str


class TerminalUI:
    def __init__(self) -> None:
        self.is_tty = sys.stdin.isatty() and sys.stdout.isatty()

    def begin(self) -> None:
        sys.stdout.write("\033[?1049h\033[?25l")
        sys.stdout.flush()

    def end(self) -> None:
        sys.stdout.write("\033[0m\033[?25h\033[?1049l")
        sys.stdout.flush()

    def clear(self) -> None:
        sys.stdout.write("\033[2J\033[H")
        sys.stdout.flush()

    def show_cursor(self) -> None:
        sys.stdout.write("\033[?25h")
        sys.stdout.flush()

    def hide_cursor(self) -> None:
        sys.stdout.write("\033[?25l")
        sys.stdout.flush()

    def center(self, text: str) -> str:
        width, _ = terminal_size()
        return (" " * max(0, (width - visible_len(text)) // 2)) + text

    def centered_lines(self, lines: list[str]) -> list[str]:
        return [self.center(line) for line in lines]

    def render(self, lines: list[str]) -> None:
        self.clear()
        sys.stdout.write("\n".join(lines))
        sys.stdout.flush()

    def panel_geometry(self, width: int) -> tuple[int, int]:
        term_width, _ = terminal_size()
        inner_width = max(30, min(width, term_width - 8))
        outer_width = inner_width + 2
        left_margin = max(0, (term_width - outer_width) // 2)
        return left_margin, inner_width

    def print_header(self) -> list[str]:
        top_rule = f"{Colors.muted}{'.' * 18}{Colors.reset} {Colors.green_dim}{'=' * 10}{Colors.reset} {Colors.cyan}{'=' * 10}{Colors.reset} {Colors.muted}{'.' * 18}{Colors.reset}"
        logo = [
            self.center(top_rule),
            self.center(f"{Colors.muted}::: {Colors.reset}{Colors.bold}{Colors.green}TELE-CLI{Colors.reset} {Colors.muted}:::{Colors.reset}"),
            self.center(f"{Colors.bold}{Colors.text}The Bridge{Colors.reset} {Colors.muted}|{Colors.reset} {Colors.green_dim}Operator Console{Colors.reset} {Colors.muted}|{Colors.reset} {Colors.cyan}v{APP_VERSION}{Colors.reset}"),
            self.center(f"{Colors.muted}telegram interface  {Colors.green_dim}<->{Colors.reset}  ai orchestration  {Colors.cyan}<->{Colors.reset}  local service{Colors.reset}"),
            "",
        ]
        return logo

    def system_strip(
        self,
        service_state: str,
        codex_state: str,
        telegram_state: str,
        summary: str,
    ) -> list[str]:
        telegram_running = telegram_state.lower() in {"paired", "running", "connected"}
        codex_running = codex_state.lower() in {"authenticated", "running"}
        service_running = service_state.lower() in {"running"}

        def status_value(is_on: bool) -> str:
            color = Colors.green if is_on else Colors.red
            word = "running" if is_on else "stopped"
            return f"{color}o{Colors.reset} {Colors.text}{word}{Colors.reset}"

        col_width = 22

        def cell(text: str) -> str:
            return text + (" " * max(0, col_width - visible_len(text)))

        title_row = "  ".join(
            [
                cell(f"{Colors.blue}{Colors.bold}Telegram{Colors.reset}"),
                cell(f"{Colors.green}{Colors.bold}AI Engine{Colors.reset}"),
                cell(f"{Colors.cyan}{Colors.bold}Tele Cli Service{Colors.reset}"),
            ]
        )
        state_row = "  ".join(
            [
                cell(status_value(telegram_running)),
                cell(status_value(codex_running)),
                cell(status_value(service_running)),
            ]
        )
        inner_width = 76
        rows = [
            f"{Colors.muted}System overview{Colors.reset}",
            "",
            title_row,
            state_row,
            "",
            f"{Colors.muted}{summary}{Colors.reset}",
        ]
        return self.panel("", rows, width=inner_width, align="left")

    def panel(self, title: str, lines: list[str], width: int = 72, align: str = "left") -> list[str]:
        _, inner_width = self.panel_geometry(width)
        if title:
            title_text = f" {title} "
            left = max(1, (inner_width - visible_len(title_text)) // 2)
            right = max(1, inner_width - visible_len(title_text) - left)
            top = f"{Colors.border}+{'-' * left}{Colors.text}{Colors.bold}{title_text}{Colors.reset}{Colors.border}{'-' * right}+{Colors.reset}"
        else:
            top = f"{Colors.border}+{'-' * inner_width}+{Colors.reset}"
        bottom = f"{Colors.border}+{'-' * inner_width}+{Colors.reset}"

        rendered = [self.center(top)]
        for line in lines:
            content_width = max(1, inner_width - 2)
            space = max(0, content_width - visible_len(line))
            if align == "center":
                left_pad = space // 2
                right_pad = space - left_pad
                content = (" " * left_pad) + line + (" " * right_pad)
            else:
                content = line + (" " * space)
            rendered.append(self.center(f"{Colors.border}|{Colors.reset} {content} {Colors.border}|{Colors.reset}"))
        rendered.append(self.center(bottom))
        return rendered

    def input_section(self, prompt: str, panel_width: int, typed: str = "", title: str = "Input") -> list[str]:
        _, inner_width = self.panel_geometry(panel_width)
        title_text = f" {title} "
        left = max(1, (inner_width - visible_len(title_text)) // 2)
        right = max(1, inner_width - visible_len(title_text) - left)
        top = f"{Colors.border}+{'-' * left}{Colors.text}{Colors.bold}{title_text}{Colors.reset}{Colors.border}{'-' * right}+{Colors.reset}"
        bottom = f"{Colors.border}+{'-' * inner_width}+{Colors.reset}"
        prompt_space = max(0, inner_width - visible_len(prompt) - 1)
        typed_space = max(0, inner_width - visible_len(typed) - 3)
        lines = [
            self.center(top),
            self.center(f"{Colors.border}|{Colors.reset} {Colors.muted}{prompt}{Colors.reset}" + (" " * prompt_space) + f"{Colors.border}|{Colors.reset}"),
            self.center(f"{Colors.border}|{Colors.reset} {Colors.green}{Colors.bold}>{Colors.reset} {Colors.text}{typed}{Colors.reset}" + (" " * typed_space) + f"{Colors.border}|{Colors.reset}"),
            self.center(bottom),
        ]
        return lines

    def spinner(self, text: str, duration: float = 0.8) -> None:
        frames = ["/", "-", "\\", "|"]
        end_at = time.time() + duration
        width, _ = terminal_size()
        index = 0
        while time.time() < end_at:
            frame = f"{Colors.cyan}{frames[index % len(frames)]}{Colors.reset} {Colors.text}{text}{Colors.reset}"
            pad = max(0, (width - visible_len(frame)) // 2)
            sys.stdout.write(f"\r\033[K{' ' * pad}{frame}")
            sys.stdout.flush()
            time.sleep(0.08)
            index += 1
        done = f"{Colors.green}*{Colors.reset} {Colors.text}{text}{Colors.reset}"
        pad = max(0, (width - visible_len(done)) // 2)
        sys.stdout.write(f"\r\033[K{' ' * pad}{done}\n")
        sys.stdout.flush()

    def splash_frame(self, frame_index: int) -> list[str]:
        rings = [
            [
                ("          .             .          ", Colors.muted),
                ("     .-====================-.     ", Colors.green_dim),
                ("   .'      SIGNAL MESH       '.   ", Colors.cyan),
                ("  /     TELEGRAM   AI CORE     \\  ", Colors.text),
                (" ;        THE BRIDGE ONLINE      ; ", Colors.green),
                (" |      LOCAL SERVICE LINKED     | ", Colors.text),
                (" ;                                ; ", Colors.green_dim),
                ("  \\        OPERATOR READY       /  ", Colors.text),
                ("   '.                        .'   ", Colors.cyan),
                ("     '-====================-'     ", Colors.green_dim),
                ("          '             '         ", Colors.muted),
            ],
            [
                ("          .     . .     .         ", Colors.muted),
                ("     .-====================-.     ", Colors.cyan),
                ("   .'      SIGNAL MESH       '.   ", Colors.green),
                ("  /     TELEGRAM   AI CORE     \\  ", Colors.text),
                (" ;        THE BRIDGE ONLINE      ; ", Colors.green),
                (" |      LOCAL SERVICE LINKED     | ", Colors.text),
                (" ;       ROUTING LIVE NOW        ; ", Colors.cyan),
                ("  \\        OPERATOR READY       /  ", Colors.text),
                ("   '.                        .'   ", Colors.green),
                ("     '-====================-'     ", Colors.cyan),
                ("         .     ' .     .          ", Colors.muted),
            ],
            [
                ("        .    .       .    .       ", Colors.muted),
                ("     .-====================-.     ", Colors.green),
                ("   .'     TELE-CLI CORE      '.   ", Colors.cyan),
                ("  /     TELEGRAM   AI CORE     \\  ", Colors.text),
                (" ;        THE BRIDGE ONLINE      ; ", Colors.green),
                (" |      LOCAL SERVICE LINKED     | ", Colors.text),
                (" ;       CHANNELS SYNCHRONIZED   ; ", Colors.green_dim),
                ("  \\        OPERATOR READY       /  ", Colors.text),
                ("   '.                        .'   ", Colors.cyan),
                ("     '-====================-'     ", Colors.green),
                ("        '    .       .    '       ", Colors.muted),
            ],
        ]
        pulse = rings[frame_index % len(rings)]
        accents = [
            f"{Colors.green_dim}::  ::  ::{Colors.reset}",
            f"{Colors.green}:: :: :: :: ::{Colors.reset}",
            f"{Colors.cyan}:: :: :: :: :: :: ::{Colors.reset}",
            f"{Colors.green}:: :: :: :: ::{Colors.reset}",
        ]
        accent = accents[frame_index % len(accents)]
        lines = [
            "",
            self.center(accent),
            "",
        ]
        for offset, (text, color) in enumerate(pulse):
            if frame_index % 2 == 1 and offset in {1, 9}:
                color = Colors.green
            if frame_index % 3 == 2 and offset in {2, 6, 8}:
                color = Colors.text
            lines.append(self.center(f"{color}{text}{Colors.reset}"))
        lines.extend(
            [
                "",
                self.center(f"{Colors.bold}{Colors.blue}Telegram{Colors.reset} {Colors.muted}<->{Colors.reset} {Colors.bold}{Colors.green}AI Engine{Colors.reset} {Colors.muted}<->{Colors.reset} {Colors.bold}{Colors.cyan}Tele Cli Service{Colors.reset}"),
                "",
                self.center(f"{Colors.muted}Establishing operator bridge across every layer{Colors.reset}"),
            ]
        )
        return lines

    def input_line(self, prompt: str, panel_width: int = 72, use_existing_field: bool = False) -> str:
        left_margin, inner_width = self.panel_geometry(panel_width)
        content_left = left_margin + 2
        max_chars = max(1, inner_width - 4)
        caret = f"{Colors.green}> {Colors.reset}"

        if use_existing_field:
            # Reuse the already-rendered input box instead of drawing a second prompt below it.
            sys.stdout.write("\033[1F")
            sys.stdout.write("\r")
            prefix = f"{Colors.border}|{Colors.reset} {caret}"
            suffix = f"{Colors.border}|{Colors.reset}"
            sys.stdout.write((" " * left_margin) + prefix)
            sys.stdout.write(" " * max_chars)
            sys.stdout.write(suffix)
            sys.stdout.write("\r")
            sys.stdout.write((" " * left_margin) + prefix)
        else:
            sys.stdout.write("\n")
            sys.stdout.write((" " * content_left) + f"{Colors.cyan}{Colors.bold}{prompt}{Colors.reset}\n")
            sys.stdout.write((" " * content_left) + caret)
        sys.stdout.flush()
        self.show_cursor()

        buffer: list[str] = []
        try:
            if sys.platform == "win32":
                import msvcrt

                while True:
                    char = msvcrt.getwch()
                    if char == "\x03":
                        raise KeyboardInterrupt
                    if char == "\x1b":
                        raise DemoExit(0)
                    if char in {"\r", "\n"}:
                        sys.stdout.write("\n")
                        sys.stdout.flush()
                        return "".join(buffer).strip()
                    if char in {"\x08", "\x7f"}:
                        if buffer:
                            buffer.pop()
                            sys.stdout.write("\b \b")
                            sys.stdout.flush()
                        continue
                    if char in {"\x00", "\xe0"}:
                        msvcrt.getwch()
                        continue
                    if char.isprintable() and len(buffer) < max_chars:
                        buffer.append(char)
                        sys.stdout.write(char)
                        sys.stdout.flush()
            else:
                import termios
                import tty

                fd = sys.stdin.fileno()
                previous = termios.tcgetattr(fd)
                try:
                    tty.setraw(fd)
                    while True:
                        char = sys.stdin.read(1)
                        if char == "\x03":
                            raise KeyboardInterrupt
                        if char == "\x1b":
                            raise DemoExit(0)
                        if char in {"\r", "\n"}:
                            sys.stdout.write("\n")
                            sys.stdout.flush()
                            return "".join(buffer).strip()
                        if char in {"\x7f", "\b"}:
                            if buffer:
                                buffer.pop()
                                sys.stdout.write("\b \b")
                                sys.stdout.flush()
                            continue
                        if char.isprintable() and len(buffer) < max_chars:
                            buffer.append(char)
                            sys.stdout.write(char)
                            sys.stdout.flush()
                finally:
                    termios.tcsetattr(fd, termios.TCSADRAIN, previous)
        finally:
            self.hide_cursor()

    def pause(self, message: str = "Press Enter to continue...") -> None:
        prompt = self.center(f"{Colors.dim}{message}{Colors.reset}")
        self.show_cursor()
        try:
            sys.stdout.write("\n" + prompt)
            sys.stdout.flush()
            input()
        except EOFError:
            pass
        except KeyboardInterrupt as exc:
            raise DemoExit(0) from exc
        finally:
            self.hide_cursor()

    def read_key(self) -> str:
        if sys.platform == "win32":
            import msvcrt

            first = msvcrt.getwch()
            if first == "\x03":
                raise KeyboardInterrupt
            if first in {"\r", "\n"}:
                return "enter"
            if first == "\x1b":
                return "esc"
            if first in {"\x00", "\xe0"}:
                second = msvcrt.getwch()
                if second == "H":
                    return "up"
                if second == "P":
                    return "down"
                return ""
            return first.lower()

        import termios
        import tty

        fd = sys.stdin.fileno()
        previous = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            first = sys.stdin.read(1)
            if first == "\x03":
                raise KeyboardInterrupt
            if first in {"\r", "\n"}:
                return "enter"
            if first == "\x1b":
                second = sys.stdin.read(1)
                if second == "[":
                    third = sys.stdin.read(1)
                    if third == "A":
                        return "up"
                    if third == "B":
                        return "down"
                return "esc"
            return first.lower()
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, previous)

    def timed_keypress(self, delay_seconds: float) -> Optional[str]:
        if sys.platform == "win32":
            import msvcrt

            end_at = time.time() + delay_seconds
            while time.time() < end_at:
                if msvcrt.kbhit():
                    key = msvcrt.getwch()
                    if key == "\x03":
                        raise KeyboardInterrupt
                    if key == "\x1b":
                        return "esc"
                    if key in {"\x00", "\xe0"}:
                        msvcrt.getwch()
                        return ""
                    return key.lower()
                time.sleep(0.02)
            return None

        import select
        import termios
        import tty

        fd = sys.stdin.fileno()
        previous = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            ready, _, _ = select.select([sys.stdin], [], [], delay_seconds)
            if not ready:
                return None
            key = sys.stdin.read(1)
            if key == "\x03":
                raise KeyboardInterrupt
            if key == "\x1b":
                return "esc"
            return key.lower()
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, previous)


class TeleCliUxDemo:
    def __init__(self) -> None:
        self.state = DemoState()
        self.ui = TerminalUI()
        self.selection = 0

    def run(self) -> None:
        if not self.ui.is_tty:
            raise SystemExit("UX demo requires an interactive terminal.")
        self.ui.begin()
        try:
            self._startup_screen()
            self._status_loop()
        except KeyboardInterrupt:
            raise DemoExit(0)
        finally:
            self.ui.end()

    def _startup_screen(self) -> None:
        self._bridge_splash_screen()
        self.ui.render(
            self.ui.print_header()
            + self.ui.system_strip("starting", "not authenticated", "not paired", "Preparing the local bridge and first-run setup.")
            + [""]
            + self.ui.panel(
                "Installing Tele-Cli",
                [
                    f"{Colors.muted}Preparing local service, dependencies, and bridge wiring.{Colors.reset}",
                ],
                width=72,
                align="center",
            )
        )
        print()
        for step in [
            "Loading local background service",
            "Synchronizing AI engine",
            "Wiring Telegram API handlers",
            "Installing required Python packages",
        ]:
            self.ui.spinner(step, 0.75)
        self.state.status_line = "Setup required"

    def _bridge_splash_screen(self) -> None:
        start = time.time()
        frame = 0
        while time.time() - start < 3.0:
            self.ui.render(self.ui.splash_frame(frame))
            time.sleep(0.16)
            frame += 1

    def _setup_intro_screen(self) -> None:
        self.ui.render(
            self.ui.print_header()
            + self.ui.panel(
                "Setup Required",
                [
                    f"{Colors.yellow}{Colors.bold}Configuration required{Colors.reset}",
                    "",
                    "Launching the first-run setup flow now.",
                ],
                align="center",
            )
        )
        time.sleep(1.2)

    def _telegram_token_screen(self) -> None:
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
            if self._is_valid_token(token):
                self.state.token = token
                self.state.status_line = "Telegram token saved"
                self.ui.render(
                    self.ui.print_header()
                    + self.ui.panel(
                        "Telegram Bot Setup",
                        [f"{Colors.green}{Colors.bold}Token saved.{Colors.reset}"],
                        align="center",
                    )
                )
                time.sleep(0.9)
                return
            error = "Invalid Telegram token. In this demo, only x fails."

    def _telegram_pairing_screen(self) -> None:
        self.state.pairing_requested = False
        self.state.pairing_code = ""
        error = ""
        while True:
            lines = [
                "Pair this machine with your Telegram user.",
                "",
                "1. Send any message to your bot.",
                "2. The bot replies with your unique code.",
                "3. Enter that code on this machine.",
            ]
            if not self.state.pairing_requested:
                lines.extend(
                    [
                        "",
                        f"{Colors.muted}Waiting for the first Telegram message.{Colors.reset}",
                        "",
                        f"{Colors.muted}Demo: press m to simulate the bot reply.{Colors.reset}",
                    ]
                )
                self.ui.render(self.ui.print_header() + self.ui.panel("Telegram Pairing", lines, width=74, align="center"))
                key = self.ui.read_key()
                if key == "m":
                    self.state.pairing_requested = True
                    self.state.pairing_code = f"{random.randint(0, 999999):06d}"
                    self.state.status_line = "Telegram replied with a unique code"
                elif key == "esc":
                    raise DemoExit(0)
                continue

            lines.extend(
                [
                    "",
                    f"{Colors.muted}Telegram replied with this code{Colors.reset}",
                    f"{Colors.green}{Colors.bold}Telegram code{Colors.reset}",
                    f"{Colors.chip_focus}   {self.state.pairing_code}   {Colors.reset}",
                    "",
                    f"{Colors.muted}Enter it below to confirm this machine.{Colors.reset}",
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
            if entered_code.lower() == "x":
                error = "Invalid pairing code. Copy the code from the Telegram bot reply."
                continue
            if entered_code != self.state.pairing_code:
                error = "Code mismatch. Enter the exact code sent by the Telegram bot."
                continue

            self.state.telegram_state = "paired"
            self.state.status_line = "Device successfully paired"
            self.ui.render(
                self.ui.print_header()
                + self.ui.panel(
                    "Telegram Pairing",
                    [f"{Colors.green}{Colors.bold}Device successfully paired.{Colors.reset}"],
                    align="center",
                )
            )
            time.sleep(0.9)
            return

    def _setup_complete_screen(self) -> None:
        self.state.configured = True
        self.state.service_state = "running"
        self.state.codex_state = "authenticated"
        self.state.telegram_state = "paired"
        self.state.status_line = "waiting for Telegram commands"
        self.ui.render(
            self.ui.print_header()
            + self.ui.panel(
                "Setup Complete",
                [
                    f"{Colors.green}{Colors.bold}Setup complete.{Colors.reset}",
                    "",
                    "Starting Tele-Cli service...",
                ],
                align="center",
            )
        )
        time.sleep(1.0)

    def _status_loop(self) -> None:
        while self.state.running:
            items = self._menu_items()
            self.selection %= len(items)
            self._render_status_screen(items)
            key = self.ui.read_key()
            if key == "up":
                self.selection = (self.selection - 1) % len(items)
            elif key == "down":
                self.selection = (self.selection + 1) % len(items)
            elif key == "enter":
                self._run_action(items[self.selection].action)
            elif key in {"q", "esc"}:
                self.state.running = False

    def _render_status_screen(self, items: list[MenuItem]) -> None:
        menu_lines: list[str] = []
        for index, item in enumerate(items):
            if index == self.selection:
                menu_lines.append(f"{Colors.chip_focus} > {item.label.ljust(28)} {Colors.reset}")
            else:
                menu_lines.append(f"  {item.label.ljust(29)}")

        self.ui.render(
            self.ui.print_header()
            + self.ui.system_strip(
                self.state.service_state,
                self.state.codex_state,
                self.state.telegram_state,
                self.state.status_line,
            )
            + [""]
            + self.ui.panel("Menu", menu_lines, width=54)
        )

    def _menu_items(self) -> list[MenuItem]:
        return [
            MenuItem("Status refresh", "refresh"),
            MenuItem("Setup", "setup"),
            MenuItem("Restart service", "service"),
            MenuItem("Debug mode", "debug"),
            MenuItem("Update Tele-Cli", "update"),
            MenuItem("Uninstall", "uninstall"),
            MenuItem("Quit", "quit"),
        ]

    def _run_action(self, action: str) -> None:
        if action == "refresh":
            self.state.status_line = "waiting for Telegram commands"
        elif action == "setup":
            self._telegram_token_screen()
            self._telegram_pairing_screen()
            self._setup_complete_screen()
        elif action == "service":
            self._service_restart_screen()
        elif action == "debug":
            self._debug_screen()
        elif action == "update":
            self._update_screen()
        elif action == "uninstall":
            self._uninstall_screen()
        elif action == "quit":
            self.state.running = False

    def _service_restart_screen(self) -> None:
        self.state.service_state = "starting"
        self.state.status_line = "service restarting after crash"
        self.ui.render(
            self.ui.print_header()
            + self.ui.panel(
                "Service Restart",
                [
                    "Existing Tele-Cli instance detected.",
                    "",
                    "Restarting the local background service now.",
                ],
                align="center",
            )
        )
        print()
        self.ui.spinner("Stopping previous instance", 0.65)
        self.ui.spinner("Starting Tele-Cli", 0.75)
        self.state.service_state = "running"
        self.state.status_line = "waiting for Telegram commands"

    def _debug_screen(self) -> None:
        frames = [
            [
                f"{Colors.blue}$ codex{Colors.reset}",
                f"{Colors.cyan}> Thinking...{Colors.reset}",
                "> Reviewing Telegram command stream",
                "> Ready for operator input",
            ],
            [
                f"{Colors.blue}$ codex{Colors.reset}",
                f"{Colors.cyan}> Thinking...{Colors.reset}",
                "> Building response draft",
                f"{Colors.green}> Waiting for next prompt{Colors.reset}",
            ],
        ]
        frame_index = 0
        while True:
            self.ui.render(
                self.ui.print_header()
                + self.ui.panel(
                    "Debug Mode",
                    [
                        f"{Colors.dim}Displaying the Codex terminal session.{Colors.reset}",
                        "",
                        *frames[frame_index % len(frames)],
                    ],
                    width=76,
                )
            )
            frame_index += 1
            key = self.ui.timed_keypress(0.85)
            if key in {"q", "esc"}:
                self.state.status_line = "returned from debug mode"
                return

    def _update_screen(self) -> None:
        self.ui.render(
            self.ui.print_header()
            + self.ui.panel(
                "Updating Tele-Cli",
                ["Preparing package refresh and service restart."],
                align="center",
            )
        )
        print()
        for step in [
            "Installing dependencies",
            "Installing Codex",
            "Creating configuration",
            "Starting Tele-Cli service",
        ]:
            self.ui.spinner(step, 0.6)
        self.state.status_line = "Tele-Cli updated successfully"

    def _uninstall_screen(self) -> None:
        self.ui.render(
            self.ui.print_header()
            + self.ui.panel(
                "Uninstall",
                [
                    f"{Colors.red}{Colors.bold}You are about to remove Tele-Cli.{Colors.reset}",
                    "",
                    "This will delete:",
                    f"{Colors.dim}~/.tele-cli{Colors.reset}",
                    f"{Colors.dim}Tele-Cli service{Colors.reset}",
                    f"{Colors.dim}tele-cli command{Colors.reset}",
                ],
                width=74,
            )
        )
        confirmation = self.ui.input_line("Type REMOVE to continue", panel_width=74)
        if confirmation != "REMOVE":
            self.state.status_line = "Uninstall cancelled"
            return

        self.ui.render(self.ui.print_header() + self.ui.panel("Uninstall", ["Removing Tele-Cli..."], align="center"))
        print()
        self.ui.spinner("Stopping Tele-Cli service", 0.55)
        self.ui.spinner("Removing files", 0.55)
        self.ui.spinner("Removing command", 0.55)
        self.ui.render(
            self.ui.print_header()
            + self.ui.panel(
                "Uninstall Complete",
                [f"{Colors.green}{Colors.bold}Tele-Cli successfully removed.{Colors.reset}"],
                align="center",
            )
        )
        self.ui.pause("Press Enter to exit the demo.")
        self.state.running = False

    def _state_chip(self, value: str) -> str:
        normalized = value.upper()
        if normalized in {"RUNNING", "AUTHENTICATED", "PAIRED"}:
            color = Colors.chip_on
        elif normalized in {"STARTING", "CONNECTING", "INSTALLING"}:
            color = Colors.chip_warn
        else:
            color = Colors.chip_off
        return f"{color} {normalized} {Colors.reset}"

    def _is_valid_token(self, token: str) -> bool:
        return token.strip().lower() != "x"


def main() -> None:
    TeleCliUxDemo().run()


if __name__ == "__main__":
    main()
