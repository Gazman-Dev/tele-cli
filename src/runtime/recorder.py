from __future__ import annotations

from pathlib import Path

from core.models import utc_now


class Recorder:
    def __init__(self, path: Path):
        self.path = path
        self.state = "STOPPED"

    def start(self) -> None:
        if self.state != "STOPPED":
            raise RuntimeError("Recorder already started.")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.state = "RUNNING"

    def record(self, source: str, line: str) -> None:
        if self.state != "RUNNING":
            raise RuntimeError("Recorder not started.")
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(f"{utc_now()} [{source}] {line}\n")

    def stop(self) -> None:
        self.state = "STOPPED"
