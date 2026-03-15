from __future__ import annotations

import os
import sys
import time
from typing import Optional

from app_meta import APP_VERSION
from .state import Colors, DemoExit, terminal_size, visible_len


class TerminalUI:
    def __init__(self) -> None:
        self._tty_streams: list[object] = []
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            self._attach_controlling_tty()
        self.is_tty = sys.stdin.isatty() and sys.stdout.isatty()

    def _attach_controlling_tty(self) -> None:
        if os.name == "nt":
            return
        try:
            tty_in = open("/dev/tty", "r", encoding=sys.stdin.encoding or "utf-8", buffering=1)
            tty_out = open("/dev/tty", "w", encoding=sys.stdout.encoding or "utf-8", buffering=1)
        except OSError:
            return
        sys.stdin = tty_in
        sys.stdout = tty_out
        sys.stderr = tty_out
        self._tty_streams = [tty_in, tty_out]

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
        top_rule = (
            f"{Colors.muted}{'.' * 16}{Colors.reset} "
            f"{Colors.green_dim}{'=' * 12}{Colors.reset} "
            f"{Colors.cyan}{'=' * 12}{Colors.reset} "
            f"{Colors.muted}{'.' * 16}{Colors.reset}"
        )
        return [
            self.center(top_rule),
            self.center(
                f"{Colors.muted}::: {Colors.reset}"
                f"{Colors.bold}{Colors.green}TELE-CLI{Colors.reset}"
                f"{Colors.muted} :::{Colors.reset}"
            ),
            self.center(
                f"{Colors.bold}{Colors.text}The Bridge{Colors.reset} "
                f"{Colors.muted}|{Colors.reset} "
                f"{Colors.green_dim}Operator Console{Colors.reset} "
                f"{Colors.muted}|{Colors.reset} "
                f"{Colors.cyan}v{APP_VERSION}{Colors.reset}"
            ),
            "",
        ]

    def system_strip(
        self,
        service_state: str,
        codex_state: str,
        telegram_state: str,
        summary: str,
    ) -> list[str]:
        telegram_running = service_state.lower() in {"running"} and telegram_state.lower() in {"running", "connected"}
        ai_service_running = service_state.lower() in {"running"} and codex_state.lower() in {"authenticated", "running"}

        def status_value(is_running: bool) -> str:
            color = Colors.green if is_running else Colors.red
            word = "running" if is_running else "error"
            return f"{color}o{Colors.reset} {Colors.text}{word}{Colors.reset}"

        def cell(text: str) -> str:
            return text + (" " * max(0, 34 - visible_len(text)))

        rows = [
            f"{Colors.muted}System overview{Colors.reset}",
            "",
            "  ".join(
                [
                    cell(f"{Colors.blue}{Colors.bold}Telegram{Colors.reset}"),
                    cell(f"{Colors.green}{Colors.bold}AI Service (Codex){Colors.reset}"),
                ]
            ),
            "  ".join(
                [
                    cell(status_value(telegram_running)),
                    cell(status_value(ai_service_running)),
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
            top = (
                f"{Colors.border}+{'-' * left}{Colors.text}{Colors.bold}{title_text}"
                f"{Colors.reset}{Colors.border}{'-' * right}+{Colors.reset}"
            )
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
            rendered.append(
                self.center(f"{Colors.border}|{Colors.reset} {content} {Colors.border}|{Colors.reset}")
            )
        rendered.append(self.center(bottom))
        return rendered

    def input_section(
        self,
        prompt: str,
        panel_width: int,
        typed: str = "",
        title: str = "Input",
    ) -> list[str]:
        _, inner_width = self.panel_geometry(panel_width)
        title_text = f" {title} "
        left = max(1, (inner_width - visible_len(title_text)) // 2)
        right = max(1, inner_width - visible_len(title_text) - left)
        top = (
            f"{Colors.border}+{'-' * left}{Colors.text}{Colors.bold}{title_text}"
            f"{Colors.reset}{Colors.border}{'-' * right}+{Colors.reset}"
        )
        bottom = f"{Colors.border}+{'-' * inner_width}+{Colors.reset}"
        prompt_space = max(0, inner_width - visible_len(prompt) - 1)
        typed_space = max(0, inner_width - visible_len(typed) - 3)
        return [
            self.center(top),
            self.center(
                f"{Colors.border}|{Colors.reset} {Colors.muted}{prompt}{Colors.reset}"
                + (" " * prompt_space)
                + f"{Colors.border}|{Colors.reset}"
            ),
            self.center(
                f"{Colors.border}|{Colors.reset} {Colors.green}{Colors.bold}>{Colors.reset} "
                f"{Colors.text}{typed}{Colors.reset}"
                + (" " * typed_space)
                + f"{Colors.border}|{Colors.reset}"
            ),
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

    def _progress_bar(self, width: int, progress: float, fill_color: str) -> str:
        progress = max(0.0, min(1.0, progress))
        filled = min(width, int(width * progress))
        if progress >= 1.0:
            filled = width
        return f"{fill_color}{'█' * filled}{Colors.reset}{Colors.muted}{'·' * (width - filled)}{Colors.reset}"

    def startup_progress_frame(
        self,
        frame_index: int,
        tasks: list[str],
        active_index: int,
        active_progress: float,
        overall_progress: float,
    ) -> list[str]:
        logo = [
            "████████╗███████╗██╗     ███████╗         ██████╗██╗     ██╗",
            "╚══██╔══╝██╔════╝██║     ██╔════╝        ██╔════╝██║     ██║",
            "   ██║   █████╗  ██║     █████╗          ██║     ██║     ██║",
            "   ██║   ██╔══╝  ██║     ██╔══╝          ██║     ██║     ██║",
            "   ██║   ███████╗███████╗███████╗        ╚██████╗███████╗██║",
            "   ╚═╝   ╚══════╝╚══════╝╚══════╝         ╚═════╝╚══════╝╚═╝",
        ]

        progress_percent = min(100, int(round(overall_progress * 100)))
        logo_width = max(len(line) for line in logo)
        filled_columns = min(logo_width, int(logo_width * overall_progress))
        if overall_progress >= 1.0:
            filled_columns = logo_width
        rendered_logo = []
        for line in logo:
            line_chars: list[str] = []
            padded_line = line.ljust(logo_width)
            for column_index, char in enumerate(padded_line):
                if column_index < filled_columns:
                    line_chars.append(f"{Colors.green}{Colors.bold}{char}{Colors.reset}")
                elif column_index == filled_columns and overall_progress < 1.0:
                    line_chars.append(f"{Colors.cyan}{Colors.bold}{char}{Colors.reset}")
                else:
                    line_chars.append(f"{Colors.muted}{char}{Colors.reset}")
            rendered_logo.append((" " * 4) + "".join(line_chars))

        status_done = f"{Colors.green}[ ready ]{Colors.reset}"
        status_live = f"{Colors.cyan}[ live  ]{Colors.reset}"
        status_wait = f"{Colors.muted}[ hold  ]{Colors.reset}"
        sys_boot = status_done if overall_progress >= 0.22 else status_live
        link_align = status_done if overall_progress >= 0.56 else status_live if overall_progress >= 0.20 else status_wait
        ai_core = status_done if overall_progress >= 0.84 else status_live if overall_progress >= 0.42 else status_wait
        dependency_status = status_done if overall_progress >= 0.999 else status_live

        def box_line(content: str) -> str:
            pad_right = max(0, 72 - visible_len(content) - 2)
            return self.center(f"{Colors.border}|{Colors.reset}  {content}{' ' * pad_right}{Colors.border}|{Colors.reset}")

        def empty_box_line() -> str:
            return self.center(f"{Colors.border}|{Colors.reset}{' ' * 72}{Colors.border}|{Colors.reset}")

        task_rows: list[str] = []
        for index, task in enumerate(tasks):
            if index < active_index:
                status = status_done
                bar = self._progress_bar(16, 1.0, Colors.green)
            elif index == active_index:
                status = status_live
                bar = self._progress_bar(16, active_progress, Colors.cyan)
            else:
                status = status_wait
                bar = self._progress_bar(16, 0.0, Colors.muted)
            task_rows.append(f"{status} {task}  {bar}")

        while len(task_rows) < 4:
            task_rows.append("")

        phase_names = ["resolving", "downloading", "wiring", "verifying"]
        phase_index = min(len(phase_names) - 1, max(0, active_index))
        phase = phase_names[phase_index]

        primary_fill = self._progress_bar(34, overall_progress, Colors.green)
        layer_a_progress = min(1.0, overall_progress / 0.34)
        layer_b_progress = min(1.0, max(0.0, (overall_progress - 0.34) / 0.33))
        layer_c_progress = min(1.0, max(0.0, (overall_progress - 0.67) / 0.33))
        layer_a = self._progress_bar(26, layer_a_progress, Colors.green_dim)
        layer_b = self._progress_bar(26, layer_b_progress, Colors.cyan)
        layer_c = self._progress_bar(26, layer_c_progress, Colors.text)

        lines = [
            "",
            self.center(f"{Colors.border}+{'-' * 72}+{Colors.reset}"),
            box_line(f"{Colors.text}{Colors.bold}BOOT.SEQ {Colors.reset}// bridge ignition{' ' * 39}{sys_boot}"),
            box_line(f"{Colors.text}{Colors.bold}NET.LINK {Colors.reset}// telegram bridge{' ' * 38}{link_align}"),
            box_line(f"{Colors.text}{Colors.bold}AI.CORE  {Colors.reset}// local reasoning stack{' ' * 34}{ai_core}"),
            box_line(f"{Colors.text}{Colors.bold}PKG.SYNC {Colors.reset}// dependency payload{' ' * 36}{dependency_status}"),
            empty_box_line(),
            box_line(
                f"{Colors.green_dim}The Bridge{Colors.reset} "
                f"{Colors.muted}|{Colors.reset} "
                f"{Colors.text}operator console activation{Colors.reset} "
                f"{Colors.muted}|{Colors.reset} "
                f"{Colors.green}{progress_percent:3d}%{Colors.reset}"
            ),
            empty_box_line(),
        ]

        for line in rendered_logo:
            lines.append(box_line(line))

        lines.extend(
            [
                empty_box_line(),
                box_line(
                    f"{Colors.muted}telegram interface{Colors.reset}  "
                    f"{Colors.green_dim}<->{Colors.reset}  "
                    f"{Colors.text}AI Service (Codex){Colors.reset}  "
                    f"{Colors.green_dim}<->{Colors.reset}  "
                    f"{Colors.cyan}operator shell{Colors.reset}"
                ),
                box_line(f"{Colors.muted}current phase:{Colors.reset} {Colors.text}{phase}{Colors.reset}"),
                empty_box_line(),
            ]
        )

        for row in task_rows:
            lines.append(box_line(row) if row else empty_box_line())

        lines.extend(
            [
                empty_box_line(),
                box_line(f"FILL LAYER A                 [{layer_a}]"),
                box_line(f"FILL LAYER B                 [{layer_b}]"),
                box_line(f"FILL LAYER C                 [{layer_c}]"),
                box_line(f"OVERALL PROGRESS             [{primary_fill} {progress_percent:3d}%]"),
                self.center(f"{Colors.border}+{'-' * 72}+{Colors.reset}"),
                "",
            ]
        )
        return lines

    def splash_frame(self, frame_index: int) -> list[str]:
        tasks = [
            "Loading AI Service (Codex)",
            "Synchronizing AI service state",
            "Wiring Telegram API handlers",
            "Installing required Python packages",
        ]
        overall = min(1.0, frame_index / 18)
        scaled = min(len(tasks), overall * len(tasks))
        active_index = min(len(tasks) - 1, int(scaled))
        active_progress = 1.0 if overall >= 1.0 else scaled - active_index
        return self.startup_progress_frame(frame_index, tasks, active_index, active_progress, overall)

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
