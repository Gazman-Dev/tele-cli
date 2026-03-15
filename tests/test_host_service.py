from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.paths import build_paths
from setup.host_service import (
    LAUNCHD_LABEL,
    SERVICE_NAME,
    LaunchdServiceManager,
    SystemdUserServiceManager,
    build_launchd_plist,
    build_service_registration,
    build_systemd_unit,
    resolve_duplicate_registrations,
)
from setup.service_manager import ServiceEnsureResult, ServiceRegistration, analyze_service_registrations
from tests.fakes.fake_service_manager import FakeServiceManager


class HostServiceTests(unittest.TestCase):
    def test_build_service_registration_uses_state_dir_in_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            with patch("platform.system", return_value="Linux"):
                registration = build_service_registration(paths)

        self.assertEqual(registration.manager, "systemd")
        self.assertEqual(registration.service_name, SERVICE_NAME)
        self.assertIn('--state-dir "', registration.executable)
        self.assertIn(str(paths.root), registration.executable)

    def test_build_systemd_unit_contains_execstart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            with patch("platform.system", return_value="Linux"):
                registration = build_service_registration(paths)

        unit = build_systemd_unit(registration)
        self.assertIn("[Service]", unit)
        self.assertIn(f"ExecStart={registration.executable}", unit)
        self.assertIn("Restart=always", unit)

    def test_build_launchd_plist_contains_label_and_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            with patch("platform.system", return_value="Darwin"):
                registration = build_service_registration(paths)

        plist = build_launchd_plist(registration)
        self.assertIn(LAUNCHD_LABEL, plist)
        self.assertIn(registration.executable, plist)
        self.assertIn("<key>EnvironmentVariables</key>", plist)
        self.assertIn("<key>PATH</key>", plist)
        self.assertIn("<key>KeepAlive</key><true/>", plist)

    def test_resolve_duplicate_registrations_repairs_when_user_accepts(self) -> None:
        manager = FakeServiceManager()
        desired = ServiceRegistration(
            manager="systemd",
            service_name="tele-cli",
            executable="/usr/bin/python -m tele_cli",
            state_dir="/srv/tele-cli",
            enabled=True,
            running=True,
        )
        manager.install(desired)
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
        analysis = analyze_service_registrations(manager.list_registrations(), desired.service_name, desired.state_dir)
        result = ServiceEnsureResult(action="repair_required", analysis=analysis)
        manager.calls.clear()

        with patch("setup.host_service.ask_choice", return_value="yes"):
            resolved = resolve_duplicate_registrations(manager, result, desired)

        self.assertTrue(resolved)
        self.assertEqual(manager.calls, [("uninstall", "tele-cli-copy")])

    def test_resolve_duplicate_registrations_returns_false_when_declined(self) -> None:
        manager = FakeServiceManager()
        desired = ServiceRegistration(
            manager="systemd",
            service_name="tele-cli",
            executable="/usr/bin/python -m tele_cli",
            state_dir="/srv/tele-cli",
            enabled=True,
            running=True,
        )
        manager.install(desired)
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
        analysis = analyze_service_registrations(manager.list_registrations(), desired.service_name, desired.state_dir)
        result = ServiceEnsureResult(action="repair_required", analysis=analysis)
        manager.calls.clear()

        with patch("setup.host_service.ask_choice", return_value="no"):
            resolved = resolve_duplicate_registrations(manager, result, desired)

        self.assertFalse(resolved)
        self.assertEqual(manager.calls, [])

    def test_systemd_list_registrations_scans_units_and_reads_enabled_running_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            unit_dir = Path(tmp)
            unit_a = unit_dir / "tele-cli.service"
            unit_b = unit_dir / "tele-cli-copy.service"
            unit_a.write_text(
                build_systemd_unit(
                    ServiceRegistration(
                        manager="systemd",
                        service_name="tele-cli",
                        executable='"/usr/bin/python" -m cli --state-dir "/srv/tele-cli" service',
                        state_dir="/srv/tele-cli",
                        enabled=True,
                        running=True,
                    )
                ),
                encoding="utf-8",
            )
            unit_b.write_text(
                build_systemd_unit(
                    ServiceRegistration(
                        manager="systemd",
                        service_name="tele-cli-copy",
                        executable='"/usr/bin/python" -m cli --state-dir "/srv/tele-cli-copy" service',
                        state_dir="/srv/tele-cli-copy",
                        enabled=True,
                        running=True,
                    )
                ),
                encoding="utf-8",
            )
            manager = SystemdUserServiceManager()
            manager.unit_dir = unit_dir

            def fake_run(args, check=False, capture_output=False, text=False, stdout=None, stderr=None):
                if "is-enabled" in args:
                    name = args[-1]
                    output = "enabled\n" if name == "tele-cli.service" else "disabled\n"
                    return subprocess.CompletedProcess(args, 0, stdout=output, stderr="")
                if "is-active" in args:
                    name = args[-1]
                    output = "active\n" if name == "tele-cli.service" else "inactive\n"
                    return subprocess.CompletedProcess(args, 0, stdout=output, stderr="")
                raise AssertionError(args)

            with patch("setup.host_service.subprocess.run", side_effect=fake_run):
                registrations = manager.list_registrations()

        self.assertEqual(len(registrations), 2)
        registrations_by_name = {registration.service_name: registration for registration in registrations}
        self.assertTrue(registrations_by_name["tele-cli"].enabled)
        self.assertTrue(registrations_by_name["tele-cli"].running)
        self.assertFalse(registrations_by_name["tele-cli-copy"].enabled)
        self.assertFalse(registrations_by_name["tele-cli-copy"].running)

    def test_launchd_list_registrations_scans_plists_and_reads_enabled_running_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            agent_dir = Path(tmp)
            plist_a = agent_dir / f"{LAUNCHD_LABEL}.plist"
            plist_b = agent_dir / "dev.gazman.tele-cli-copy.plist"
            plist_a.write_text(
                build_launchd_plist(
                    ServiceRegistration(
                        manager="launchd",
                        service_name=LAUNCHD_LABEL,
                        executable='"/usr/bin/python" -m cli --state-dir "/srv/tele-cli" service',
                        state_dir="/srv/tele-cli",
                        enabled=True,
                        running=True,
                    )
                ),
                encoding="utf-8",
            )
            plist_b.write_text(
                build_launchd_plist(
                    ServiceRegistration(
                        manager="launchd",
                        service_name="dev.gazman.tele-cli-copy",
                        executable='"/usr/bin/python" -m cli --state-dir "/srv/tele-cli-copy" service',
                        state_dir="/srv/tele-cli-copy",
                        enabled=True,
                        running=True,
                    )
                ),
                encoding="utf-8",
            )
            manager = LaunchdServiceManager()
            manager.agent_dir = agent_dir

            def fake_run(args, check=False, capture_output=False, text=False, stdout=None, stderr=None):
                joined = " ".join(args)
                if "print-disabled" in joined:
                    output = '{"dev.gazman.tele-cli" => false, "dev.gazman.tele-cli-copy" => true}\n'
                    return subprocess.CompletedProcess(args, 0, stdout=output, stderr="")
                if "print gui/" in joined and "tele-cli-copy" in joined:
                    return subprocess.CompletedProcess(args, 0, stdout="state = waiting\n", stderr="")
                if "print gui/" in joined and joined.endswith(f"/{LAUNCHD_LABEL}"):
                    return subprocess.CompletedProcess(args, 0, stdout="state = running\npid = 123\n", stderr="")
                raise AssertionError(args)

            with patch("setup.host_service.subprocess.run", side_effect=fake_run), patch(
                "setup.host_service.os.getuid", return_value=501, create=True
            ):
                registrations = manager.list_registrations()

        self.assertEqual(len(registrations), 2)
        registrations_by_name = {registration.service_name: registration for registration in registrations}
        self.assertTrue(registrations_by_name[LAUNCHD_LABEL].enabled)
        self.assertTrue(registrations_by_name[LAUNCHD_LABEL].running)
        self.assertFalse(registrations_by_name["dev.gazman.tele-cli-copy"].enabled)
        self.assertFalse(registrations_by_name["dev.gazman.tele-cli-copy"].running)

    def test_systemd_manager_install_start_stop_restart_uninstall_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            unit_dir = Path(tmp)
            manager = SystemdUserServiceManager()
            manager.unit_dir = unit_dir
            registration = ServiceRegistration(
                manager="systemd",
                service_name="tele-cli",
                executable='"/usr/bin/python" -m cli --state-dir "/srv/tele-cli" service',
                state_dir="/srv/tele-cli",
                enabled=True,
                running=True,
            )
            commands: list[list[str]] = []

            def fake_run(args, check=True, stdout=None, stderr=None, capture_output=False, text=False):
                commands.append(list(args))
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

            with patch("setup.host_service.subprocess.run", side_effect=fake_run):
                manager.install(registration)
                manager.start("tele-cli")
                manager.stop("tele-cli")
                manager.restart("tele-cli")
                manager.uninstall("tele-cli")

            unit_path = unit_dir / "tele-cli.service"
            self.assertFalse(unit_path.exists())
            self.assertIn(["systemctl", "--user", "daemon-reload"], commands)
            self.assertIn(["systemctl", "--user", "enable", "tele-cli.service"], commands)
            self.assertIn(["systemctl", "--user", "start", "tele-cli.service"], commands)
            self.assertIn(["systemctl", "--user", "stop", "tele-cli.service"], commands)
            self.assertIn(["systemctl", "--user", "restart", "tele-cli.service"], commands)
            self.assertGreaterEqual(commands.count(["systemctl", "--user", "daemon-reload"]), 2)

    def test_launchd_manager_install_start_stop_restart_uninstall_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            agent_dir = Path(tmp)
            manager = LaunchdServiceManager()
            manager.agent_dir = agent_dir
            registration = ServiceRegistration(
                manager="launchd",
                service_name=LAUNCHD_LABEL,
                executable='"/usr/bin/python" -m cli --state-dir "/srv/tele-cli" service',
                state_dir="/srv/tele-cli",
                enabled=True,
                running=True,
            )
            commands: list[list[str]] = []

            def fake_run(args, check=True, stdout=None, stderr=None, capture_output=False, text=False):
                commands.append(list(args))
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

            with patch("setup.host_service.subprocess.run", side_effect=fake_run), patch(
                "setup.host_service.os.getuid", return_value=501, create=True
            ):
                manager.install(registration)
                manager.start(LAUNCHD_LABEL)
                manager.stop(LAUNCHD_LABEL)
                manager.restart(LAUNCHD_LABEL)
                plist_path = agent_dir / f"{LAUNCHD_LABEL}.plist"
                self.assertTrue(plist_path.exists())
                manager.uninstall(LAUNCHD_LABEL)

            self.assertFalse((agent_dir / f"{LAUNCHD_LABEL}.plist").exists())
            self.assertIn(
                ["launchctl", "bootstrap", "gui/501", str(agent_dir / f"{LAUNCHD_LABEL}.plist")],
                commands,
            )
            self.assertIn(["launchctl", "enable", f"gui/501/{LAUNCHD_LABEL}"], commands)
            self.assertIn(["launchctl", "kickstart", "-k", f"gui/501/{LAUNCHD_LABEL}"], commands)
            self.assertIn(["launchctl", "bootout", f"gui/501/{LAUNCHD_LABEL}"], commands)


if __name__ == "__main__":
    unittest.main()
