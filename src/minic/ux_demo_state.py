from __future__ import annotations

import re
import shutil
from dataclasses import dataclass


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
