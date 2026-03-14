from __future__ import annotations

import sys
import time
from typing import Optional

from . import APP_VERSION
from .ux_demo_state import Colors, DemoExit, terminal_size, visible_len


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
        return [
            self.center(top_rule),
            self.center(f"{Colors.muted}::: {Colors.reset}{Colors.bold}{Colors.green}TELE-CLI{Colors.reset} {Colors.muted}:::{Colors.reset}"),
            self.center(f"{Colors.bold}{Colors.text}The Bridge{Colors.reset} {Colors.muted}|{Colors.reset} {Colors.green_dim}Operator Console{Colors.reset} {Colors.muted}|{Colors.reset} {Colors.cyan}v{APP_VERSION}{Colors.reset}"),
            self.center(f"{Colors.muted}telegram interface  {Colors.green_dim}<->{Colors.reset}  ai orchestration  {Colors.cyan}<->{Colors.reset}  local service{Colors.reset}"),
            "",
        ]

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

        def cell(text: str) -> str:
            return text + (" " * max(0, 22 - visible_len(text)))

        rows = [
            f"{Colors.muted}System overview{Colors.reset}",
            "",
            "  ".join(
                [
                    cell(f"{Colors.blue}{Colors.bold}Telegram{Colors.reset}"),
                    cell(f"{Colors.green}{Colors.bold}AI Engine{Colors.reset}"),
                    cell(f"{Colors.cyan}{Colors.bold}Tele Cli Service{Colors.reset}"),
                ]
            ),
            "  ".join(
                [
                    cell(status_value(telegram_running)),
                    cell(status_value(codex_running)),
                    cell(status_value(service_running)),
                ]
            ),
            "",
            f"{Colors.muted}{summary}{Colors.reset}",
        ]
        return self.panel("", rows, width=76, align="left")

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
        return [
            self.center(top),
            self.center(f"{Colors.border}|{Colors.reset} {Colors.muted}{prompt}{Colors.reset}" + (" " * prompt_space) + f"{Colors.border}|{Colors.reset}"),
            self.center(f"{Colors.border}|{Colors.reset} {Colors.green}{Colors.bold}>{Colors.reset} {Colors.text}{typed}{Colors.reset}" + (" " * typed_space) + f"{Colors.border}|{Colors.reset}"),
            self.center(bottom),
        ]

    def spinner(self, text: str, duration: float = 0.8) -> None:
        frames = ["/", "-", "\\", "|"]
        end_at = time.time() + duration
        width, _ = terminal_size()
        index = 0
        while time.time() < end_at:
            frame = f"{Colors.cyan}{frames[index % len(frames)]}{Colors.reset} {Colors.text}{text}{Colors.reset}"
            sys.stdout.write(f"\r\033[K{' ' * max(0, (width - visible_len(frame)) // 2)}{frame}")
            sys.stdout.flush()
            time.sleep(0.08)
            index += 1
        done = f"{Colors.green}*{Colors.reset} {Colors.text}{text}{Colors.reset}"
        sys.stdout.write(f"\r\033[K{' ' * max(0, (width - visible_len(done)) // 2)}{done}\n")
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
        accents = [
            f"{Colors.green_dim}::  ::  ::{Colors.reset}",
            f"{Colors.green}:: :: :: :: ::{Colors.reset}",
            f"{Colors.cyan}:: :: :: :: :: :: ::{Colors.reset}",
            f"{Colors.green}:: :: :: :: ::{Colors.reset}",
        ]
        lines = ["", self.center(accents[frame_index % len(accents)]), ""]
        for offset, (text, color) in enumerate(rings[frame_index % len(rings)]):
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
