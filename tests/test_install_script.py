from __future__ import annotations

import unittest
from pathlib import Path


class InstallScriptTests(unittest.TestCase):
    def test_install_script_fetches_setup_via_cache_busting_raw_url(self) -> None:
        script = (Path(__file__).resolve().parents[1] / "install.sh").read_text(encoding="utf-8")

        self.assertIn("raw.githubusercontent.com", script)
        self.assertIn("setup.sh", script)
        self.assertIn("Cache-Control: no-cache", script)
        self.assertIn("Pragma: no-cache", script)
        self.assertIn("source=install-wrapper&ts=${CACHE_BUSTER}", script)
        self.assertIn("exec bash \"$TMP_SCRIPT\" \"$@\"", script)


if __name__ == "__main__":
    unittest.main()
