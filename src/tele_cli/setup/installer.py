from __future__ import annotations

import platform
import shutil
import subprocess
from dataclasses import dataclass
from typing import Optional


class InstallerError(RuntimeError):
    pass


@dataclass
class InstallPlan:
    manager: Optional[str]
    command: list[str]


class InstallerStrategy:
    def detect_package_manager(self) -> Optional[str]:
        raise NotImplementedError

    def install_npm(self, allow_homebrew_install: bool = False) -> InstallPlan:
        raise NotImplementedError

    def install_codex(self) -> InstallPlan:
        if not shutil.which("npm"):
            raise InstallerError("npm is required before installing Codex.")
        return InstallPlan(manager="npm", command=["npm", "install", "-g", "@openai/codex"])

    def run(self, plan: InstallPlan) -> None:
        subprocess.run(plan.command, check=True)


class LinuxInstallerStrategy(InstallerStrategy):
    def detect_package_manager(self) -> Optional[str]:
        for name in ("apt", "dnf", "yum", "pacman", "zypper"):
            if shutil.which(name):
                return name
        return None

    def install_npm(self, allow_homebrew_install: bool = False) -> InstallPlan:
        manager = self.detect_package_manager()
        if manager == "apt":
            return InstallPlan(manager, ["sudo", "apt", "install", "-y", "npm"])
        if manager == "dnf":
            return InstallPlan(manager, ["sudo", "dnf", "install", "-y", "npm"])
        if manager == "yum":
            return InstallPlan(manager, ["sudo", "yum", "install", "-y", "npm"])
        if manager == "pacman":
            return InstallPlan(manager, ["sudo", "pacman", "-S", "--noconfirm", "npm"])
        if manager == "zypper":
            return InstallPlan(manager, ["sudo", "zypper", "--non-interactive", "install", "npm"])
        raise InstallerError("No supported Linux package manager found for npm installation.")


class MacOSInstallerStrategy(InstallerStrategy):
    def detect_package_manager(self) -> Optional[str]:
        return "brew" if shutil.which("brew") else None

    def install_npm(self, allow_homebrew_install: bool = False) -> InstallPlan:
        if shutil.which("brew"):
            return InstallPlan("brew", ["brew", "install", "node"])
        if allow_homebrew_install:
            return InstallPlan(
                "brew-bootstrap",
                ["/bin/bash", "-c", "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"],
            )
        raise InstallerError("Homebrew is required on macOS to install npm in V1.")


def current_installer() -> InstallerStrategy:
    system = platform.system()
    if system == "Linux":
        return LinuxInstallerStrategy()
    if system == "Darwin":
        return MacOSInstallerStrategy()
    raise InstallerError(f"Unsupported OS for V1: {system}")
