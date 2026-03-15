from __future__ import annotations

from setup.service_manager import ServiceRegistration


class FakeServiceManager:
    def __init__(self) -> None:
        self._registrations: list[ServiceRegistration] = []
        self.calls: list[tuple[str, str]] = []

    def install(self, registration: ServiceRegistration) -> None:
        self.calls.append(("install", registration.service_name))
        self._registrations = [
            existing for existing in self._registrations if existing.service_name != registration.service_name
        ]
        self._registrations.append(registration)

    def install_duplicate(self, registration: ServiceRegistration) -> None:
        self._registrations.append(registration)

    def start(self, service_name: str) -> None:
        self.calls.append(("start", service_name))
        self._replace(
            service_name,
            lambda registration: ServiceRegistration(
                manager=registration.manager,
                service_name=registration.service_name,
                executable=registration.executable,
                state_dir=registration.state_dir,
                enabled=True,
                running=True,
            ),
        )

    def stop(self, service_name: str) -> None:
        self.calls.append(("stop", service_name))
        self._replace(
            service_name,
            lambda registration: ServiceRegistration(
                manager=registration.manager,
                service_name=registration.service_name,
                executable=registration.executable,
                state_dir=registration.state_dir,
                enabled=registration.enabled,
                running=False,
            ),
        )

    def restart(self, service_name: str) -> None:
        self.calls.append(("restart", service_name))
        self._replace(
            service_name,
            lambda registration: ServiceRegistration(
                manager=registration.manager,
                service_name=registration.service_name,
                executable=registration.executable,
                state_dir=registration.state_dir,
                enabled=True,
                running=True,
            ),
        )

    def uninstall(self, service_name: str) -> None:
        self.calls.append(("uninstall", service_name))
        self._registrations = [
            registration for registration in self._registrations if registration.service_name != service_name
        ]

    def list_registrations(self) -> list[ServiceRegistration]:
        return list(self._registrations)

    def _replace(self, service_name: str, transform) -> None:
        for index, registration in enumerate(self._registrations):
            if registration.service_name == service_name:
                self._registrations[index] = transform(registration)
                return
        raise KeyError(service_name)
