from __future__ import annotations

from core.models import RuntimeState


class ServiceRuntime:
    def __init__(self, state: RuntimeState):
        self.state = state

    def start_telegram(self) -> None:
        self._transition("telegram_state", "STOPPED", "RUNNING")

    def start_recorder(self) -> None:
        self._transition("recorder_state", "STOPPED", "RUNNING")

    def start_debug(self) -> None:
        self._transition("debug_state", "STOPPED", "RUNNING")

    def start_codex(self) -> None:
        self._transition("codex_state", "STOPPED", "RUNNING")

    def set_codex_state(self, new_state: str) -> None:
        self.state.codex_state = new_state

    def stop_codex(self) -> None:
        self.state.codex_state = "STOPPED"
        self.state.codex_pid = None

    def _transition(self, field: str, expected: str, new_state: str) -> None:
        current = getattr(self.state, field)
        if current != expected:
            raise RuntimeError(f"{field} already in state {current}.")
        setattr(self.state, field, new_state)
