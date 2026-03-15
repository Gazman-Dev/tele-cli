from __future__ import annotations

import subprocess
import threading

from .jsonrpc import JsonRpcTransport


class SubprocessJsonRpcTransport(JsonRpcTransport):
    def __init__(self, process: subprocess.Popen[str]):
        self.process = process
        self._write_lock = threading.Lock()

    @classmethod
    def start(cls, command: list[str]) -> "SubprocessJsonRpcTransport":
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        return cls(process)

    def read_line(self, timeout: float | None = None) -> str | None:
        if self.process.stdout is None:
            return None
        line = self.process.stdout.readline()
        if line == "":
            return None
        return line.rstrip("\n")

    def write_line(self, line: str) -> None:
        if self.process.stdin is None:
            raise RuntimeError("app server stdin is not available")
        with self._write_lock:
            self.process.stdin.write(line + "\n")
            self.process.stdin.flush()

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
