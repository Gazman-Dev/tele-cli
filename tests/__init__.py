from __future__ import annotations

import sys
import shutil
import tempfile
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

TEST_TMP = ROOT / ".tmp-tests"
TEST_TMP.mkdir(parents=True, exist_ok=True)
tempfile.tempdir = str(TEST_TMP)


class WorkspaceTemporaryDirectory:
    def __init__(self) -> None:
        self.name = str(TEST_TMP / f"tmp{uuid.uuid4().hex}")

    def __enter__(self) -> str:
        Path(self.name).mkdir(parents=True, exist_ok=False)
        return self.name

    def __exit__(self, exc_type, exc, tb) -> None:
        shutil.rmtree(self.name, ignore_errors=True)


tempfile.TemporaryDirectory = WorkspaceTemporaryDirectory
