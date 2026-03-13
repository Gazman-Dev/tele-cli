from __future__ import annotations

import unittest
from unittest.mock import patch

from minic.installer import LinuxInstallerStrategy, MacOSInstallerStrategy


class InstallerTests(unittest.TestCase):
    def test_linux_detects_apt(self) -> None:
        strategy = LinuxInstallerStrategy()
        with patch("shutil.which", side_effect=lambda name: "/usr/bin/apt" if name == "apt" else None):
            self.assertEqual(strategy.detect_package_manager(), "apt")
            self.assertEqual(strategy.install_npm().command, ["sudo", "apt", "install", "-y", "npm"])

    def test_macos_uses_brew(self) -> None:
        strategy = MacOSInstallerStrategy()
        with patch("shutil.which", side_effect=lambda name: "/opt/homebrew/bin/brew" if name == "brew" else None):
            self.assertEqual(strategy.detect_package_manager(), "brew")
            self.assertEqual(strategy.install_npm().command, ["brew", "install", "node"])


if __name__ == "__main__":
    unittest.main()
