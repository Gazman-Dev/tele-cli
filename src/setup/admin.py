from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

from core.paths import AppPaths
from core.prompts import ask_text
from .host_service import build_service_registration, current_service_manager, resolve_duplicate_registrations
from .service_manager import perform_service_update

REPO_URL = "https://github.com/Gazman-Dev/tele-cli.git"
USER_BIN_DIR = Path.home() / ".local" / "bin"
SERVICE_NAME = "tele-cli"
LAUNCHD_LABEL = "dev.gazman.tele-cli"


def run_update(paths: AppPaths | None = None) -> None:
    print("Updating Tele Cli...")
    if paths is None:
        _run_package_update()
    else:
        manager = current_service_manager()
        desired = build_service_registration(paths)
        result = perform_service_update(manager, desired, _run_package_update)
        if not resolve_duplicate_registrations(manager, result, desired):
            raise SystemExit("Update cancelled because duplicate service registrations were not repaired.")
        if result.action == "repair_required":
            result = perform_service_update(manager, desired, _run_package_update)
    print("Update complete.")


def run_uninstall(paths: AppPaths) -> None:
    confirmation = ask_text("Type uninstall to confirm removal")
    if confirmation.strip() != "uninstall":
        raise SystemExit("Uninstall cancelled.")
    uninstall(paths)
    raise SystemExit(0)


def uninstall(paths: AppPaths) -> None:
    print("Removing Tele Cli...")
    _remove_service(paths)
    _remove_package()
    _remove_launchers()
    shutil.rmtree(paths.root, ignore_errors=True)
    print("Uninstall complete.")


def _remove_service(paths: AppPaths) -> None:
    system = platform.system()
    if system == "Darwin":
        _remove_launchd_service()
    elif system == "Linux":
        _remove_systemd_user_service()
        _remove_fallback_service(paths)


def _run_package_update() -> None:
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--quiet",
            "--upgrade",
            "--force-reinstall",
            "--no-cache-dir",
            f"git+{REPO_URL}",
        ],
        check=True,
    )


def _remove_launchd_service() -> None:
    plist_path = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
    if plist_path.exists():
        subprocess.run(
            ["launchctl", "bootout", f"gui/{os.getuid()}", str(plist_path)],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        plist_path.unlink(missing_ok=True)


def _remove_systemd_user_service() -> None:
    unit_path = Path.home() / ".config" / "systemd" / "user" / f"{SERVICE_NAME}.service"
    try:
        subprocess.run(
            ["systemctl", "--user", "stop", f"{SERVICE_NAME}.service"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        subprocess.run(
            ["systemctl", "--user", "disable", f"{SERVICE_NAME}.service"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        subprocess.run(
            ["systemctl", "--user", "daemon-reload"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        pass
    unit_path.unlink(missing_ok=True)


def _remove_fallback_service(paths: AppPaths) -> None:
    pid_path = paths.root / "service.pid"
    runner_path = paths.root / "run-service.sh"
    if pid_path.exists():
        try:
            pid = int(pid_path.read_text(encoding="utf-8").strip())
        except ValueError:
            pid = 0
        if pid > 0:
            try:
                os.kill(pid, 15)
            except OSError:
                pass
        pid_path.unlink(missing_ok=True)
    runner_path.unlink(missing_ok=True)


def _remove_package() -> None:
    subprocess.run(
        [sys.executable, "-m", "pip", "uninstall", "-y", "tele-cli"],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _remove_launchers() -> None:
    (USER_BIN_DIR / "tele-cli").unlink(missing_ok=True)
