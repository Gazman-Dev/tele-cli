from __future__ import annotations


class DebugMirror:
    def __init__(self) -> None:
        self.state = "STOPPED"

    def start(self) -> None:
        if self.state != "STOPPED":
            raise RuntimeError("Debug mirror already started.")
        self.state = "RUNNING"

    def emit(self, source: str, line: str) -> None:
        if self.state != "RUNNING":
            raise RuntimeError("Debug mirror not started.")
        print(f"[{source}] {line}")

    def stop(self) -> None:
        self.state = "STOPPED"
