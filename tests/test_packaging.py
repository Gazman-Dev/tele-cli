from __future__ import annotations

import tomllib
import unittest
from pathlib import Path


class PackagingTests(unittest.TestCase):
    def test_pyproject_includes_top_level_entry_modules(self) -> None:
        pyproject = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8"))
        modules = pyproject["tool"]["setuptools"]["py-modules"]

        self.assertIn("cli", modules)
        self.assertIn("app_shell", modules)
        self.assertIn("app_meta", modules)


if __name__ == "__main__":
    unittest.main()
