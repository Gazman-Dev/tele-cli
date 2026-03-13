from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, TypeVar

T = TypeVar("T")


def load_json(path: Path, factory: Callable[[dict], T]) -> T | None:
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return factory(data)


def save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
