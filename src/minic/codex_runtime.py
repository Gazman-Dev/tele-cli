from __future__ import annotations

import subprocess
import threading
from collections.abc import Callable
from typing import Optional


class CodexSession:
    def __init__(self, command: list[str], on_output: Callable[[str, str], None]):
        self.command = command
        self.on_output = on_output
        self.process = None  # type: Optional[subprocess.Popen]
        self.state = "STOPPED"
        self._threads: list[threading.Thread] = []

    def start(self) -> int:
        if self.state != "STOPPED":
            raise RuntimeError("Codex session already started.")
        self.process = subprocess.Popen(
            self.command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self.state = "RUNNING"
        self._threads = [
            threading.Thread(target=self._pump, args=("stdout", self.process.stdout), daemon=True),
            threading.Thread(target=self._pump, args=("stderr", self.process.stderr), daemon=True),
        ]
        for thread in self._threads:
            thread.start()
        return int(self.process.pid)

    def send(self, text: str) -> None:
        if self.state != "RUNNING" or not self.process or not self.process.stdin:
            raise RuntimeError("Codex session is not running.")
        self.process.stdin.write(text + "\n")
        self.process.stdin.flush()

    def stop(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.terminate()
        self.state = "STOPPED"

    def _pump(self, source: str, stream) -> None:
        if stream is None:
            return
        for line in iter(stream.readline, ""):
            self.on_output(source, line.rstrip())
