from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class ServiceRegistration:
    manager: str
    service_name: str
    executable: str
    state_dir: str
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


class ServiceManager(Protocol):
    def list_registrations(self) -> list[ServiceRegistration]:
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
