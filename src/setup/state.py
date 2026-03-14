from __future__ import annotations

from typing import Optional

from core.json_store import load_json, save_json
from core.models import SetupState
from core.paths import AppPaths


def load_setup_state(paths: AppPaths) -> Optional[SetupState]:
    return load_json(paths.setup_lock, SetupState.from_dict)


def save_setup_state(paths: AppPaths, state: SetupState) -> None:
    save_json(paths.setup_lock, state.to_dict())
