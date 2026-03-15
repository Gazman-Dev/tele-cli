from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.paths import build_paths
from setup.admin import run_update
from setup.service_manager import ServiceRegistration
from tests.fakes.fake_service_manager import FakeServiceManager


class AdminTests(unittest.TestCase):
    def test_run_update_uses_managed_service_update_when_paths_are_provided(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            manager = FakeServiceManager()
            update_calls: list[str] = []

            with (
                patch("setup.admin.current_service_manager", return_value=manager),
                patch("setup.admin._run_package_update", side_effect=lambda: update_calls.append("updated")),
            ):
                run_update(paths)

        self.assertEqual(update_calls, ["updated"])
        self.assertEqual(
            manager.calls,
            [("install", "tele-cli"), ("start", "tele-cli")],
        )

    def test_run_update_without_paths_runs_package_update_only(self) -> None:
        update_calls: list[str] = []

        with patch("setup.admin._run_package_update", side_effect=lambda: update_calls.append("updated")):
            run_update()

        self.assertEqual(update_calls, ["updated"])

    def test_run_update_repairs_duplicate_registration_and_continues(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            manager = FakeServiceManager()
            manager.install(
                ServiceRegistration(
                    manager="systemd",
                    service_name="tele-cli",
                    executable="/usr/bin/python -m tele_cli",
                    state_dir=str(paths.root),
                    enabled=True,
                    running=True,
                )
            )
            manager.install_duplicate(
                ServiceRegistration(
                    manager="launchd",
                    service_name="tele-cli-copy",
                    executable="/usr/local/bin/tele-cli",
                    state_dir=str(paths.root),
                    enabled=True,
                    running=False,
                )
            )
            manager.calls.clear()
            update_calls: list[str] = []

            with (
                patch("setup.admin.current_service_manager", return_value=manager),
                patch("setup.admin._run_package_update", side_effect=lambda: update_calls.append("updated")),
                patch("setup.host_service.ask_choice", return_value="yes"),
            ):
                run_update(paths)

        self.assertEqual(update_calls, ["updated"])
        self.assertEqual(
            manager.calls,
            [
                ("uninstall", "tele-cli-copy"),
                ("stop", "tele-cli"),
                ("install", "tele-cli"),
                ("start", "tele-cli"),
            ],
        )

    def test_run_update_aborts_when_duplicate_repair_is_declined(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            manager = FakeServiceManager()
            manager.install(
                ServiceRegistration(
                    manager="systemd",
                    service_name="tele-cli",
                    executable="/usr/bin/python -m tele_cli",
                    state_dir=str(paths.root),
                    enabled=True,
                    running=True,
                )
            )
            manager.install_duplicate(
                ServiceRegistration(
                    manager="launchd",
                    service_name="tele-cli-copy",
                    executable="/usr/local/bin/tele-cli",
                    state_dir=str(paths.root),
                    enabled=True,
                    running=False,
                )
            )
            manager.calls.clear()

            with (
                patch("setup.admin.current_service_manager", return_value=manager),
                patch("setup.host_service.ask_choice", return_value="no"),
            ):
                with self.assertRaises(SystemExit):
                    run_update(paths)

        self.assertEqual(manager.calls, [])


if __name__ == "__main__":
    unittest.main()
