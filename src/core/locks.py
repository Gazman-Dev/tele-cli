from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .json_store import load_json, save_json
from .models import LockMetadata
from .process import is_same_app_process, process_exists


@dataclass
class LockInspection:
    exists: bool
    live: bool = False
    same_app: bool = False
    metadata: Optional[LockMetadata] = None


class LockFile:
    def __init__(self, path: Path):
        self.path = path

    def read(self) -> Optional[LockMetadata]:
        return load_json(self.path, LockMetadata.from_dict)

    def write(self, metadata: LockMetadata) -> None:
        save_json(self.path, metadata.to_dict())

    def clear(self) -> None:
        if self.path.exists():
            self.path.unlink()

    def inspect(self) -> LockInspection:
        metadata = self.read()
        if not metadata:
            return LockInspection(exists=False)
        live = process_exists(metadata.pid)
        same_app = live and is_same_app_process(metadata)
        return LockInspection(exists=True, live=live, same_app=same_app, metadata=metadata)
