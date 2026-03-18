from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from runtime.codex_cli_config import read_codex_cli_preferences, write_codex_cli_preferences


class CodexCliConfigTests(unittest.TestCase):
    def test_write_and_read_preferences(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"

            model, reasoning = write_codex_cli_preferences(path=path, model="gpt-5.4-mini", reasoning="low")

            self.assertEqual((model, reasoning), ("gpt-5.4-mini", "low"))
            self.assertEqual(read_codex_cli_preferences(path), ("gpt-5.4-mini", "low"))

    def test_write_preferences_preserves_unrelated_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text('sandbox_mode = "danger-full-access"\nmodel = "old"\n', encoding="utf-8")

            write_codex_cli_preferences(path=path, reasoning="medium")

            text = path.read_text(encoding="utf-8")
            self.assertIn('sandbox_mode = "danger-full-access"', text)
            self.assertIn('model = "old"', text)
            self.assertIn('model_reasoning_effort = "medium"', text)

    def test_write_preferences_can_persist_runtime_policy_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"

            write_codex_cli_preferences(path=path, approval_policy="never", sandbox_mode="danger-full-access")

            text = path.read_text(encoding="utf-8")
            self.assertIn('approval_policy = "never"', text)
            self.assertIn('sandbox_mode = "danger-full-access"', text)


if __name__ == "__main__":
    unittest.main()
