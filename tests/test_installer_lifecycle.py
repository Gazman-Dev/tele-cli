from __future__ import annotations

import unittest
from pathlib import Path

from setup.service_manager import (
    ServiceRegistration,
    analyze_service_registrations,
    choose_canonical_registration,
    ensure_service_registration,
    perform_service_update,
    repair_duplicate_registrations,
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

    def test_ensure_service_registration_installs_and_starts_missing_service(self) -> None:
        manager = FakeServiceManager()
        desired = ServiceRegistration(
            manager="systemd",
            service_name="tele-cli",
            executable="/usr/bin/python -m tele_cli",
            state_dir="/srv/tele-cli",
            enabled=True,
            running=True,
        )

        result = ensure_service_registration(manager, desired)

        self.assertEqual(result.action, "installed")
        self.assertFalse(result.analysis.has_duplicates)
        self.assertEqual(manager.calls, [("install", "tele-cli"), ("start", "tele-cli")])
        registrations = manager.list_registrations()
        self.assertEqual(len(registrations), 1)
        self.assertTrue(registrations[0].running)

    def test_ensure_service_registration_updates_existing_canonical_service(self) -> None:
        manager = FakeServiceManager()
        manager.install(
            ServiceRegistration(
                manager="systemd",
                service_name="tele-cli",
                executable="/usr/bin/python -m tele_cli",
                state_dir="/srv/tele-cli",
                enabled=True,
                running=False,
            )
        )
        manager.calls.clear()
        desired = ServiceRegistration(
            manager="systemd",
            service_name="tele-cli",
            executable="/usr/local/bin/tele-cli",
            state_dir="/srv/tele-cli",
            enabled=True,
            running=True,
        )

        result = ensure_service_registration(manager, desired)

        self.assertEqual(result.action, "updated")
        self.assertFalse(result.analysis.has_duplicates)
        self.assertEqual(manager.calls, [("install", "tele-cli"), ("restart", "tele-cli")])
        registrations = manager.list_registrations()
        self.assertEqual(len(registrations), 1)
        self.assertEqual(registrations[0].executable, "/usr/local/bin/tele-cli")
        self.assertTrue(registrations[0].running)

    def test_ensure_service_registration_updates_launchd_when_environment_path_changes(self) -> None:
        manager = FakeServiceManager()
        manager.install(
            ServiceRegistration(
                manager="launchd",
                service_name="dev.gazman.tele-cli",
                executable="/usr/local/bin/tele-cli",
                state_dir="/srv/tele-cli",
                environment_path="/usr/bin:/bin:/usr/sbin:/sbin",
                enabled=True,
                running=True,
            )
        )
        manager.calls.clear()
        desired = ServiceRegistration(
            manager="launchd",
            service_name="dev.gazman.tele-cli",
            executable="/usr/local/bin/tele-cli",
            state_dir="/srv/tele-cli",
            environment_path="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
            enabled=True,
            running=True,
        )

        result = ensure_service_registration(manager, desired)

        self.assertEqual(result.action, "updated")
        self.assertEqual(manager.calls, [("install", "dev.gazman.tele-cli"), ("restart", "dev.gazman.tele-cli")])
        registrations = manager.list_registrations()
        self.assertEqual(
            registrations[0].environment_path,
            "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        )

    def test_ensure_service_registration_requires_repair_when_duplicates_exist(self) -> None:
        manager = FakeServiceManager()
        manager.install(
            ServiceRegistration(
                manager="systemd",
                service_name="tele-cli",
                executable="/usr/bin/python -m tele_cli",
                state_dir="/srv/tele-cli",
                enabled=True,
                running=True,
            )
        )
        manager.install_duplicate(
            ServiceRegistration(
                manager="launchd",
                service_name="tele-cli-copy",
                executable="/usr/local/bin/tele-cli",
                state_dir="/srv/tele-cli",
                enabled=True,
                running=False,
            )
        )
        manager.calls.clear()

        result = ensure_service_registration(
            manager,
            ServiceRegistration(
                manager="systemd",
                service_name="tele-cli",
                executable="/usr/bin/python -m tele_cli",
                state_dir="/srv/tele-cli",
                enabled=True,
                running=True,
            ),
        )

        self.assertEqual(result.action, "repair_required")
        self.assertTrue(result.analysis.has_duplicates)
        self.assertEqual(manager.calls, [])

    def test_repair_duplicate_registrations_keeps_canonical_and_removes_others(self) -> None:
        manager = FakeServiceManager()
        manager.install(
            ServiceRegistration(
                manager="systemd",
                service_name="tele-cli",
                executable="/usr/bin/python -m tele_cli",
                state_dir="/srv/tele-cli",
                enabled=True,
                running=False,
            )
        )
        manager.install_duplicate(
            ServiceRegistration(
                manager="launchd",
                service_name="tele-cli-copy",
                executable="/usr/local/bin/tele-cli",
                state_dir="/srv/tele-cli",
                enabled=True,
                running=True,
            )
        )
        manager.calls.clear()

        result = repair_duplicate_registrations(manager, "tele-cli", "/srv/tele-cli")

        self.assertEqual(len(result.removed), 1)
        self.assertEqual(result.removed[0].service_name, "tele-cli-copy")
        self.assertFalse(result.analysis.has_duplicates)
        self.assertEqual(manager.calls, [("uninstall", "tele-cli-copy"), ("start", "tele-cli")])
        registrations = manager.list_registrations()
        self.assertEqual(len(registrations), 1)
        self.assertEqual(registrations[0].service_name, "tele-cli")
        self.assertTrue(registrations[0].running)

    def test_perform_service_update_stops_updates_and_restarts_canonical_service(self) -> None:
        manager = FakeServiceManager()
        manager.install(
            ServiceRegistration(
                manager="systemd",
                service_name="tele-cli",
                executable="/usr/bin/python -m tele_cli",
                state_dir="/srv/tele-cli",
                enabled=True,
                running=True,
            )
        )
        manager.calls.clear()
        applied: list[str] = []

        result = perform_service_update(
            manager,
            ServiceRegistration(
                manager="systemd",
                service_name="tele-cli",
                executable="/usr/local/bin/tele-cli",
                state_dir="/srv/tele-cli",
                enabled=True,
                running=True,
            ),
            lambda: applied.append("updated"),
        )

        self.assertEqual(result.action, "updated")
        self.assertFalse(result.analysis.has_duplicates)
        self.assertEqual(applied, ["updated"])
        self.assertEqual(
            manager.calls,
            [("stop", "tele-cli"), ("install", "tele-cli"), ("start", "tele-cli")],
        )
        registrations = manager.list_registrations()
        self.assertEqual(registrations[0].executable, "/usr/local/bin/tele-cli")
        self.assertTrue(registrations[0].running)

    def test_perform_service_update_requires_repair_when_duplicates_exist(self) -> None:
        manager = FakeServiceManager()
        manager.install(
            ServiceRegistration(
                manager="systemd",
                service_name="tele-cli",
                executable="/usr/bin/python -m tele_cli",
                state_dir="/srv/tele-cli",
                enabled=True,
                running=True,
            )
        )
        manager.install_duplicate(
            ServiceRegistration(
                manager="launchd",
                service_name="tele-cli-copy",
                executable="/usr/local/bin/tele-cli",
                state_dir="/srv/tele-cli",
                enabled=True,
                running=False,
            )
        )
        manager.calls.clear()
        applied: list[str] = []

        result = perform_service_update(
            manager,
            ServiceRegistration(
                manager="systemd",
                service_name="tele-cli",
                executable="/usr/local/bin/tele-cli",
                state_dir="/srv/tele-cli",
                enabled=True,
                running=True,
            ),
            lambda: applied.append("updated"),
        )

        self.assertEqual(result.action, "repair_required")
        self.assertTrue(result.analysis.has_duplicates)
        self.assertEqual(applied, [])
        self.assertEqual(manager.calls, [])


if __name__ == "__main__":
    unittest.main()
