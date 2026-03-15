from __future__ import annotations

from setup.service_manager import ServiceRegistration


class FakeServiceManager:
    def __init__(self) -> None:
        self._registrations: dict[str, ServiceRegistration] = {}

    def install(self, registration: ServiceRegistration) -> None:
        self._registrations[registration.service_name] = registration

    def start(self, service_name: str) -> None:
        registration = self._registrations[service_name]
        self._registrations[service_name] = ServiceRegistration(
            manager=registration.manager,
            service_name=registration.service_name,
            executable=registration.executable,
            state_dir=registration.state_dir,
            enabled=True,
            running=True,
        )

    def stop(self, service_name: str) -> None:
        registration = self._registrations[service_name]
        self._registrations[service_name] = ServiceRegistration(
            manager=registration.manager,
            service_name=registration.service_name,
            executable=registration.executable,
            state_dir=registration.state_dir,
            enabled=registration.enabled,
            running=False,
        )

    def restart(self, service_name: str) -> None:
        self.stop(service_name)
        self.start(service_name)

    def uninstall(self, service_name: str) -> None:
        self._registrations.pop(service_name, None)

    def list_registrations(self) -> list[ServiceRegistration]:
        return list(self._registrations.values())
