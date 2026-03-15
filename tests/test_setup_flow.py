from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.json_store import save_json
from core.models import Config, SetupState
from core.paths import build_paths
from setup.setup_flow import ensure_local_dependencies


class _FakeInstaller:
    def __init__(self) -> None:
        self.ran = []

    def install_npm(self, allow_homebrew_install: bool = False):
        class Plan:
            manager = "brew"
            command = ["brew", "install", "node"]

        return Plan()

    def install_codex(self):
        class Plan:
            manager = "npm"
            command = ["npm", "install", "-g", "@openai/codex"]

        return Plan()

    def run(self, plan) -> None:
        self.ran.append(list(plan.command))


class SetupFlowTests(unittest.TestCase):
    def test_ensure_local_dependencies_installs_missing_npm_and_codex(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            save_json(paths.config, Config(state_dir=str(paths.root)).to_dict())
            state = SetupState(status="started", pid=1, timestamp="now")
            fake_installer = _FakeInstaller()

            with (
                patch("setup.setup_flow.current_installer", return_value=fake_installer),
                patch("setup.setup_flow.shutil.which", side_effect=lambda name: None),
            ):
                steps = ensure_local_dependencies(paths, state)

            self.assertEqual(
                steps,
                ["Installing npm via brew", "Installing Codex CLI"],
            )
            self.assertEqual(
                fake_installer.ran,
                [["brew", "install", "node"], ["npm", "install", "-g", "@openai/codex"]],
            )
            self.assertTrue(state.npm_installed)
            self.assertTrue(state.codex_installed)


if __name__ == "__main__":
    unittest.main()
