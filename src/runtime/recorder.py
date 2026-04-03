from __future__ import annotations

import re
from pathlib import Path

from core.models import utc_now
from storage.operations import TraceStore


class Recorder:
    def __init__(self, path: Path, *, trace_store: TraceStore | None = None, mirror_to_file: bool = True):
        self.path = path
        self.trace_store = trace_store
        self.mirror_to_file = mirror_to_file
        self.state = "STOPPED"

    def start(self) -> None:
        if self.state != "STOPPED":
            raise RuntimeError("Recorder already started.")
        if self.mirror_to_file:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.state = "RUNNING"

    def record(self, source: str, line: str) -> None:
        if self.state != "RUNNING":
            raise RuntimeError("Recorder not started.")
        if self.trace_store is not None:
            normalized_source = re.sub(r"[^a-zA-Z0-9._-]+", "_", source.strip()).strip("_") or "unknown"
            self.trace_store.log_event(
                source="terminal",
                event_type=f"terminal.{normalized_source}",
                payload={"source": source, "line": line},
            )
        if self.mirror_to_file:
            try:
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(f"{utc_now()} [{source}] {line}\n")
            except OSError as exc:
                if self.trace_store is None:
                    raise
                self.trace_store.log_event(
                    source="storage",
                    event_type="logging.mirror_write_failed",
                    payload={"mirror": self.path.name, "error": str(exc)},
                )

    def stop(self) -> None:
        self.state = "STOPPED"
