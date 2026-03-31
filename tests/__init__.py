from __future__ import annotations

import os
import subprocess
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


def _install_fast_workspace_git_stub() -> None:
    if os.environ.get("TELE_CLI_TEST_REAL_GIT") == "1":
        return

    from runtime.workspaces import WorkspaceManager

    if getattr(WorkspaceManager, "_tele_cli_test_git_stub_installed", False):
        return

    def fake_git(self, cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
        git_dir = cwd / ".git"
        committed_marker = git_dir / ".tele_cli_committed"
        command = tuple(args)
        stdout = ""
        stderr = ""
        returncode = 0

        if command == ("init",):
            git_dir.mkdir(parents=True, exist_ok=True)
        elif command == ("status", "--porcelain"):
            stdout = "" if committed_marker.exists() else "?? staged-by-test-stub\n"
        elif command[:2] == ("rev-parse", "HEAD"):
            if not git_dir.exists():
                returncode = 1
                stderr = "fatal: not a git repository"
            else:
                stdout = "0000000000000000000000000000000000000001\n"
        elif command[:1] == ("commit",):
            git_dir.mkdir(parents=True, exist_ok=True)
            committed_marker.write_text("committed\n", encoding="utf-8")
        elif command[:1] in {("add",), ("config",), ("update-index",), ("push",)}:
            git_dir.mkdir(parents=True, exist_ok=True)
        elif command == ("remote",):
            stdout = ""

        return subprocess.CompletedProcess(
            ["git", *args],
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
        )

    WorkspaceManager._git = fake_git
    WorkspaceManager._tele_cli_test_git_stub_installed = True


_install_fast_workspace_git_stub()
