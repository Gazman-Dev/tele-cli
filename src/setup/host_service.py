from __future__ import annotations

import os
import platform
import subprocess
import sys
from pathlib import Path
from xml.etree import ElementTree as ET

from core.paths import AppPaths
from core.prompts import ask_choice

from .service_manager import (
    ServiceEnsureResult,
    ServiceManager,
    ServiceRegistration,
    ServiceUpdateResult,
    repair_duplicate_registrations,
)

SERVICE_NAME = "tele-cli"
LAUNCHD_LABEL = "dev.gazman.tele-cli"


def launchd_path() -> str:
    segments = [
        os.environ.get("PATH", ""),
        "/opt/homebrew/bin",
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
        "/usr/sbin",
        "/sbin",
    ]
    ordered: list[str] = []
    for segment in segments:
        for entry in segment.split(":"):
            if entry and entry not in ordered:
                ordered.append(entry)
    return ":".join(ordered)


def build_service_command(paths: AppPaths) -> str:
    return f'"{sys.executable}" -m cli --state-dir "{paths.root}" service'


def build_service_registration(paths: AppPaths) -> ServiceRegistration:
    executable = build_service_command(paths)
    system = platform.system()
    manager = "launchd" if system == "Darwin" else "systemd"
    service_name = LAUNCHD_LABEL if manager == "launchd" else SERVICE_NAME
    return ServiceRegistration(
        manager=manager,
        service_name=service_name,
        executable=executable,
        state_dir=str(paths.root),
        environment_path=launchd_path() if manager == "launchd" else os.environ.get("PATH", ""),
        enabled=True,
        running=True,
    )


class SystemdUserServiceManager(ServiceManager):
    def __init__(self) -> None:
        self.unit_dir = Path.home() / ".config" / "systemd" / "user"

    def list_registrations(self) -> list[ServiceRegistration]:
        registrations: list[ServiceRegistration] = []
        if not self.unit_dir.exists():
            return registrations
        for unit_path in self.unit_dir.glob("*.service"):
            executable = extract_exec_start(unit_path) or ""
            state_dir = extract_state_dir(executable)
            if not executable or not state_dir:
                continue
            service_name = unit_path.stem
            registrations.append(
                ServiceRegistration(
                    manager="systemd",
                    service_name=service_name,
                    executable=executable,
                    state_dir=state_dir,
                    environment_path=None,
                    enabled=self._is_enabled(service_name),
                    running=self._is_running(service_name),
                )
            )
        return registrations

    def install(self, registration: ServiceRegistration) -> None:
        self.unit_dir.mkdir(parents=True, exist_ok=True)
        unit_path = self.unit_dir / f"{SERVICE_NAME}.service"
        unit_path.write_text(build_systemd_unit(registration), encoding="utf-8")
        self._run("systemctl", "--user", "daemon-reload")
        self._run("systemctl", "--user", "enable", f"{SERVICE_NAME}.service")

    def start(self, service_name: str) -> None:
        self._run("systemctl", "--user", "start", f"{service_name}.service")

    def stop(self, service_name: str) -> None:
        self._run("systemctl", "--user", "stop", f"{service_name}.service")

    def restart(self, service_name: str) -> None:
        self._run("systemctl", "--user", "restart", f"{service_name}.service")

    def uninstall(self, service_name: str) -> None:
        self._run("systemctl", "--user", "stop", f"{service_name}.service", check=False)
        self._run("systemctl", "--user", "disable", f"{service_name}.service", check=False)
        unit_path = self.unit_dir / f"{service_name}.service"
        unit_path.unlink(missing_ok=True)
        self._run("systemctl", "--user", "daemon-reload", check=False)

    @staticmethod
    def _run(*args: str, check: bool = True) -> None:
        subprocess.run(list(args), check=check, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _query(self, *args: str) -> str:
        result = subprocess.run(
            list(args),
            check=False,
            capture_output=True,
            text=True,
        )
        return (result.stdout or "").strip()

    def _is_enabled(self, service_name: str) -> bool:
        status = self._query("systemctl", "--user", "is-enabled", f"{service_name}.service")
        return status == "enabled"

    def _is_running(self, service_name: str) -> bool:
        status = self._query("systemctl", "--user", "is-active", f"{service_name}.service")
        return status == "active"


class LaunchdServiceManager(ServiceManager):
    def __init__(self) -> None:
        self.agent_dir = Path.home() / "Library" / "LaunchAgents"

    def list_registrations(self) -> list[ServiceRegistration]:
        registrations: list[ServiceRegistration] = []
        if not self.agent_dir.exists():
            return registrations
        for plist_path in self.agent_dir.glob("*.plist"):
            label = extract_launchd_label(plist_path)
            executable = extract_program_arguments(plist_path)
            state_dir = extract_state_dir(executable)
            if not label or not executable or not state_dir:
                continue
            registrations.append(
                ServiceRegistration(
                    manager="launchd",
                    service_name=label,
                    executable=executable,
                    state_dir=state_dir,
                    environment_path=extract_launchd_path(plist_path),
                    enabled=not self._is_disabled(label),
                    running=self._is_running(label),
                )
            )
        return registrations

    def install(self, registration: ServiceRegistration) -> None:
        self.agent_dir.mkdir(parents=True, exist_ok=True)
        plist_path = self.agent_dir / f"{LAUNCHD_LABEL}.plist"
        plist_path.write_text(build_launchd_plist(registration), encoding="utf-8")
        self._run("launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist_path), check=False)
        self._run("launchctl", "enable", f"gui/{os.getuid()}/{LAUNCHD_LABEL}", check=False)

    def start(self, service_name: str) -> None:
        self._run("launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{service_name}")

    def stop(self, service_name: str) -> None:
        self._run("launchctl", "bootout", f"gui/{os.getuid()}/{service_name}", check=False)

    def restart(self, service_name: str) -> None:
        self.start(service_name)

    def uninstall(self, service_name: str) -> None:
        plist_path = self.agent_dir / f"{service_name}.plist"
        self._run("launchctl", "bootout", f"gui/{os.getuid()}/{service_name}", check=False)
        plist_path.unlink(missing_ok=True)

    @staticmethod
    def _run(*args: str, check: bool = True) -> None:
        subprocess.run(list(args), check=check, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _query(self, *args: str) -> str:
        result = subprocess.run(
            list(args),
            check=False,
            capture_output=True,
            text=True,
        )
        return (result.stdout or "").strip()

    def _is_disabled(self, service_name: str) -> bool:
        output = self._query("launchctl", "print-disabled", f"gui/{os.getuid()}")
        disabled_marker = f'"{service_name}" => true'
        legacy_marker = f'"{service_name}" = true'
        return disabled_marker in output or legacy_marker in output

    def _is_running(self, service_name: str) -> bool:
        output = self._query("launchctl", "print", f"gui/{os.getuid()}/{service_name}")
        lowered = output.lower()
        return "state = running" in lowered or "pid =" in lowered


def current_service_manager() -> ServiceManager:
    system = platform.system()
    if system == "Linux":
        return SystemdUserServiceManager()
    if system == "Darwin":
        return LaunchdServiceManager()
    raise RuntimeError(f"Unsupported OS for service manager: {system}")


def resolve_duplicate_registrations(
    manager: ServiceManager,
    result: ServiceEnsureResult | ServiceUpdateResult,
    desired: ServiceRegistration,
) -> bool:
    if result.action != "repair_required":
        return True
    duplicates = ", ".join(
        f"{registration.service_name} ({registration.manager})" for registration in result.analysis.duplicates
    )
    choice = ask_choice(
        f"Duplicate Tele Cli services detected for {desired.state_dir}: {duplicates}. Remove the extra registration(s) and continue?",
        ["yes", "no"],
        default="yes",
    )
    if choice != "yes":
        return False
    repair_duplicate_registrations(manager, desired.service_name, desired.state_dir)
    return True


def build_systemd_unit(registration: ServiceRegistration) -> str:
    return (
        "[Unit]\n"
        "Description=Tele Cli\n\n"
        "[Service]\n"
        f"ExecStart={registration.executable}\n"
        "Restart=always\n"
        "RestartSec=5\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def build_launchd_plist(registration: ServiceRegistration) -> str:
    program = registration.executable.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    path_value = launchd_path().replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return (
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
        "<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" "
        "\"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">\n"
        "<plist version=\"1.0\">\n"
        "<dict>\n"
        f"  <key>Label</key><string>{registration.service_name}</string>\n"
        "  <key>ProgramArguments</key>\n"
        "  <array>\n"
        f"    <string>/bin/sh</string>\n"
        f"    <string>-lc</string>\n"
        f"    <string>{program}</string>\n"
        "  </array>\n"
        "  <key>EnvironmentVariables</key>\n"
        "  <dict>\n"
        f"    <key>PATH</key><string>{path_value}</string>\n"
        "  </dict>\n"
        "  <key>RunAtLoad</key><true/>\n"
        "  <key>KeepAlive</key><true/>\n"
        "</dict>\n"
        "</plist>\n"
    )


def extract_exec_start(unit_path: Path) -> str | None:
    for line in unit_path.read_text(encoding="utf-8").splitlines():
        if line.startswith("ExecStart="):
            return line.split("=", 1)[1].strip()
    return None


def extract_program_arguments(plist_path: Path) -> str:
    try:
        root = ET.fromstring(plist_path.read_text(encoding="utf-8"))
    except ET.ParseError:
        return ""
    values = plist_dict_values(root)
    program_arguments = values.get("ProgramArguments")
    if not isinstance(program_arguments, list) or len(program_arguments) < 3:
        return ""
    return str(program_arguments[-1])


def extract_launchd_label(plist_path: Path) -> str:
    try:
        root = ET.fromstring(plist_path.read_text(encoding="utf-8"))
    except ET.ParseError:
        return ""
    values = plist_dict_values(root)
    label = values.get("Label")
    return str(label) if isinstance(label, str) else ""


def extract_launchd_path(plist_path: Path) -> str:
    try:
        root = ET.fromstring(plist_path.read_text(encoding="utf-8"))
    except ET.ParseError:
        return ""
    values = plist_dict_values(root)
    env = values.get("EnvironmentVariables")
    if not isinstance(env, dict):
        return ""
    path_value = env.get("PATH")
    return str(path_value) if isinstance(path_value, str) else ""


def plist_dict_values(root: ET.Element) -> dict[str, object]:
    dict_element = root.find("dict")
    if dict_element is None:
        return {}
    values: dict[str, object] = {}
    children = list(dict_element)
    index = 0
    while index + 1 < len(children):
        key_element = children[index]
        value_element = children[index + 1]
        index += 2
        if key_element.tag != "key":
            continue
        key = key_element.text or ""
        if value_element.tag == "string":
            values[key] = value_element.text or ""
        elif value_element.tag == "array":
            values[key] = [item.text or "" for item in value_element if item.tag == "string"]
        elif value_element.tag == "dict":
            nested: dict[str, object] = {}
            nested_children = list(value_element)
            nested_index = 0
            while nested_index + 1 < len(nested_children):
                nested_key_element = nested_children[nested_index]
                nested_value_element = nested_children[nested_index + 1]
                nested_index += 2
                if nested_key_element.tag != "key":
                    continue
                nested_key = nested_key_element.text or ""
                if nested_value_element.tag == "string":
                    nested[nested_key] = nested_value_element.text or ""
                elif nested_value_element.tag == "true":
                    nested[nested_key] = True
                elif nested_value_element.tag == "false":
                    nested[nested_key] = False
            values[key] = nested
        elif value_element.tag == "true":
            values[key] = True
        elif value_element.tag == "false":
            values[key] = False
    return values


def extract_state_dir(executable: str) -> str:
    marker = '--state-dir "'
    if marker not in executable:
        return ""
    tail = executable.split(marker, 1)[1]
    return tail.split('"', 1)[0]
