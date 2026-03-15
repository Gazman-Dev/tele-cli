from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol


@dataclass(frozen=True)
class ServiceRegistration:
    manager: str
    service_name: str
    executable: str
    state_dir: str
    environment_path: str | None = None
    enabled: bool = True
    running: bool = False


@dataclass(frozen=True)
class ServiceRegistrationAnalysis:
    state_dir: str
    canonical: ServiceRegistration | None
    duplicates: tuple[ServiceRegistration, ...]

    @property
    def has_duplicates(self) -> bool:
        return bool(self.duplicates)


@dataclass(frozen=True)
class ServiceEnsureResult:
    action: str
    analysis: ServiceRegistrationAnalysis


@dataclass(frozen=True)
class ServiceRepairResult:
    analysis: ServiceRegistrationAnalysis
    removed: tuple[ServiceRegistration, ...]


@dataclass(frozen=True)
class ServiceUpdateResult:
    action: str
    analysis: ServiceRegistrationAnalysis


class ServiceManager(Protocol):
    def list_registrations(self) -> list[ServiceRegistration]:
        raise NotImplementedError

    def install(self, registration: ServiceRegistration) -> None:
        raise NotImplementedError

    def start(self, service_name: str) -> None:
        raise NotImplementedError

    def stop(self, service_name: str) -> None:
        raise NotImplementedError

    def restart(self, service_name: str) -> None:
        raise NotImplementedError

    def uninstall(self, service_name: str) -> None:
        raise NotImplementedError


def analyze_service_registrations(
    registrations: list[ServiceRegistration],
    service_name: str,
    state_dir: str | Path,
) -> ServiceRegistrationAnalysis:
    normalized_state_dir = str(Path(state_dir).expanduser().resolve())
    matching = [
        registration
        for registration in registrations
        if Path(registration.state_dir).expanduser().resolve() == Path(normalized_state_dir)
    ]
    canonical = choose_canonical_registration(matching, service_name)
    duplicates = tuple(registration for registration in matching if registration != canonical)
    return ServiceRegistrationAnalysis(
        state_dir=normalized_state_dir,
        canonical=canonical,
        duplicates=duplicates,
    )


def choose_canonical_registration(
    registrations: list[ServiceRegistration],
    service_name: str,
) -> ServiceRegistration | None:
    if not registrations:
        return None
    ranked = sorted(
        registrations,
        key=lambda registration: (
            registration.service_name != service_name,
            not registration.enabled,
            not registration.running,
            registration.manager,
            registration.executable,
        ),
    )
    return ranked[0]


def ensure_service_registration(
    manager: ServiceManager,
    desired: ServiceRegistration,
) -> ServiceEnsureResult:
    analysis = analyze_service_registrations(
        manager.list_registrations(),
        desired.service_name,
        desired.state_dir,
    )
    if analysis.has_duplicates:
        return ServiceEnsureResult(action="repair_required", analysis=analysis)
    canonical = analysis.canonical
    if canonical is None:
        manager.install(desired)
        if desired.running:
            manager.start(desired.service_name)
        refreshed = analyze_service_registrations(
            manager.list_registrations(),
            desired.service_name,
            desired.state_dir,
        )
        return ServiceEnsureResult(action="installed", analysis=refreshed)
    if canonical == desired:
        if desired.running and not canonical.running:
            manager.start(canonical.service_name)
            action = "started"
        elif not desired.running and canonical.running:
            manager.stop(canonical.service_name)
            action = "stopped"
        else:
            action = "unchanged"
        refreshed = analyze_service_registrations(
            manager.list_registrations(),
            desired.service_name,
            desired.state_dir,
        )
        return ServiceEnsureResult(action=action, analysis=refreshed)
    manager.install(desired)
    if desired.running:
        manager.restart(desired.service_name)
    else:
        manager.stop(desired.service_name)
    refreshed = analyze_service_registrations(
        manager.list_registrations(),
        desired.service_name,
        desired.state_dir,
    )
    return ServiceEnsureResult(action="updated", analysis=refreshed)


def repair_duplicate_registrations(
    manager: ServiceManager,
    service_name: str,
    state_dir: str | Path,
) -> ServiceRepairResult:
    analysis = analyze_service_registrations(manager.list_registrations(), service_name, state_dir)
    removed: list[ServiceRegistration] = []
    for duplicate in analysis.duplicates:
        manager.uninstall(duplicate.service_name)
        removed.append(duplicate)
    canonical = analysis.canonical
    if canonical is not None and not canonical.running:
        manager.start(canonical.service_name)
    refreshed = analyze_service_registrations(manager.list_registrations(), service_name, state_dir)
    return ServiceRepairResult(
        analysis=refreshed,
        removed=tuple(removed),
    )


def perform_service_update(
    manager: ServiceManager,
    desired: ServiceRegistration,
    apply_update: Callable[[], None],
) -> ServiceUpdateResult:
    analysis = analyze_service_registrations(
        manager.list_registrations(),
        desired.service_name,
        desired.state_dir,
    )
    if analysis.has_duplicates:
        return ServiceUpdateResult(action="repair_required", analysis=analysis)
    canonical = analysis.canonical
    if canonical is not None and canonical.running:
        manager.stop(canonical.service_name)
    apply_update()
    manager.install(desired)
    if desired.running:
        manager.start(desired.service_name)
    refreshed = analyze_service_registrations(
        manager.list_registrations(),
        desired.service_name,
        desired.state_dir,
    )
    return ServiceUpdateResult(
        action="updated" if canonical is not None else "installed",
        analysis=refreshed,
    )
