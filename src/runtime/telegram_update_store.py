from __future__ import annotations

from dataclasses import dataclass, field

from core.json_store import load_json, save_json
from core.paths import AppPaths


@dataclass
class TelegramUpdateStoreState:
    processed_update_ids: list[int] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"processed_update_ids": self.processed_update_ids}

    @classmethod
    def from_dict(cls, data: dict) -> "TelegramUpdateStoreState":
        return cls(processed_update_ids=[int(item) for item in data.get("processed_update_ids", [])])


class TelegramUpdateStore:
    def __init__(self, paths: AppPaths, *, max_entries: int = 1024):
        self.paths = paths
        self.max_entries = max_entries

    def load(self) -> TelegramUpdateStoreState:
        return load_json(self.paths.telegram_updates, TelegramUpdateStoreState.from_dict) or TelegramUpdateStoreState()

    def save(self, state: TelegramUpdateStoreState) -> None:
        save_json(self.paths.telegram_updates, state.to_dict())

    def has_processed(self, update_id: int) -> bool:
        state = self.load()
        return update_id in state.processed_update_ids

    def mark_processed(self, update_id: int) -> bool:
        state = self.load()
        if update_id in state.processed_update_ids:
            return False
        state.processed_update_ids.append(update_id)
        if len(state.processed_update_ids) > self.max_entries:
            state.processed_update_ids = state.processed_update_ids[-self.max_entries :]
        self.save(state)
        return True
