from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Callable, Optional, TypeVar

T = TypeVar("T")


def load_json(path: Path, factory: Callable[[dict], T]) -> Optional[T]:
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return factory(data)


def save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, indent=2, sort_keys=True)
    fd, temp_path_str = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
        text=True,
    )
    temp_path = Path(temp_path_str)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        temp_path.replace(path)
        try:
            dir_fd = os.open(str(path.parent), os.O_RDONLY)
        except OSError:
            dir_fd = None
        if dir_fd is not None:
            try:
                os.fsync(dir_fd)
            except OSError:
                pass
            finally:
                os.close(dir_fd)
    finally:
        if temp_path.exists():
            temp_path.unlink(missing_ok=True)
