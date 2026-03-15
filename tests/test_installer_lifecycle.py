from __future__ import annotations

import unittest
from pathlib import Path

from setup.service_manager import (
    ServiceRegistration,
    analyze_service_registrations,
    choose_canonical_registration,
)
from tests.fakes.fake_service_manager import FakeServiceManager


class InstallerLifecycleTests(unittest.TestCase):
    def test_fake_service_manager_replaces_existing_registration_by_service_name(self) -> None:
        manager = FakeServiceManager()
        manager.install(
            ServiceRegistration(
                manager="systemd",
                service_name="tele-cli",
                executable="/usr/bin/python -m tele_cli",
                state_dir="/srv/tele-cli-a",
                enabled=True,
                running=False,
            )
        )
        manager.install(
            ServiceRegistration(
                manager="systemd",
                service_name="tele-cli",
                executable="/usr/local/bin/tele-cli",
                state_dir="/srv/tele-cli-b",
                enabled=True,
                running=False,
            )
        )

        registrations = manager.list_registrations()

        self.assertEqual(len(registrations), 1)
        self.assertEqual(registrations[0].state_dir, "/srv/tele-cli-b")

    def test_choose_canonical_registration_prefers_expected_service_name(self) -> None:
        registrations = [
            ServiceRegistration(
                manager="systemd",
                service_name="tele-cli-copy",
                executable="/usr/bin/python -m tele_cli",
                state_dir="/tmp/tele-cli",
                enabled=True,
                running=True,
            ),
            ServiceRegistration(
                manager="systemd",
                service_name="tele-cli",
                executable="/usr/bin/python -m tele_cli",
                state_dir="/tmp/tele-cli",
                enabled=True,
                running=True,
            ),
        ]

        canonical = choose_canonical_registration(registrations, "tele-cli")

        self.assertIsNotNone(canonical)
        self.assertEqual(canonical.service_name, "tele-cli")

    def test_choose_canonical_registration_prefers_enabled_running_service(self) -> None:
        registrations = [
            ServiceRegistration(
                manager="systemd",
                service_name="tele-cli",
                executable="/usr/bin/python -m tele_cli",
                state_dir="/tmp/tele-cli",
                enabled=False,
                running=False,
            ),
            ServiceRegistration(
                manager="launchd",
                service_name="tele-cli",
                executable="/usr/local/bin/tele-cli",
                state_dir="/tmp/tele-cli",
                enabled=True,
                running=True,
            ),
        ]

        canonical = choose_canonical_registration(registrations, "tele-cli")

        self.assertIsNotNone(canonical)
        self.assertEqual(canonical.manager, "launchd")

    def test_analyze_service_registrations_detects_duplicates_for_same_state_dir(self) -> None:
        state_dir = Path("/srv/tele-cli")
        registrations = [
            ServiceRegistration(
                manager="systemd",
                service_name="tele-cli",
                executable="/usr/bin/python -m tele_cli",
                state_dir=str(state_dir),
                enabled=True,
                running=True,
            ),
            ServiceRegistration(
                manager="launchd",
                service_name="tele-cli-copy",
                executable="/usr/local/bin/tele-cli",
                state_dir=str(state_dir),
                enabled=True,
                running=False,
            ),
            ServiceRegistration(
                manager="systemd",
                service_name="tele-cli-other",
                executable="/usr/bin/python -m tele_cli",
                state_dir=str(state_dir.parent / "other"),
                enabled=True,
                running=True,
            ),
        ]

        analysis = analyze_service_registrations(registrations, "tele-cli", state_dir)

        self.assertIsNotNone(analysis.canonical)
        self.assertEqual(analysis.canonical.service_name, "tele-cli")
        self.assertTrue(analysis.has_duplicates)
        self.assertEqual(len(analysis.duplicates), 1)
        self.assertEqual(analysis.duplicates[0].service_name, "tele-cli-copy")


if __name__ == "__main__":
    unittest.main()
