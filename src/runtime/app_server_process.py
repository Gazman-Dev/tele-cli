from __future__ import annotations

import subprocess
import queue
import threading

from .jsonrpc import JsonRpcTransport


class SubprocessJsonRpcTransport(JsonRpcTransport):
    def __init__(self, process: subprocess.Popen[str]):
        self.process = process
        self._write_lock = threading.Lock()
        self._stdout_lines: queue.Queue[str | None] = queue.Queue()
        self._stdout_thread = threading.Thread(
            target=self._pump_lines,
            args=(self.process.stdout, self._stdout_lines),
            daemon=True,
        )
        self._stderr_thread = threading.Thread(
            target=self._drain_stream,
            args=(self.process.stderr,),
            daemon=True,
        )
        self._stdout_thread.start()
        self._stderr_thread.start()

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
        try:
            line = self._stdout_lines.get(timeout=timeout)
        except queue.Empty:
            return None
        if line is None:
            return None
        return line

    def write_line(self, line: str) -> None:
        if self.process.stdin is None:
            raise RuntimeError("app server stdin is not available")
        with self._write_lock:
            self.process.stdin.write(line + "\n")
            self.process.stdin.flush()

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()

    def is_alive(self) -> bool:
        return self.process.poll() is None

    @staticmethod
    def _pump_lines(stream, sink: queue.Queue[str | None]) -> None:
        if stream is None:
            sink.put(None)
            return
        while True:
            line = stream.readline()
            if line == "":
                sink.put(None)
                return
            sink.put(line.rstrip("\n"))

    @staticmethod
    def _drain_stream(stream) -> None:
        if stream is None:
            return
        while True:
            line = stream.readline()
            if line == "":
                return
