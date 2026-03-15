from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.paths import build_paths
from runtime.telegram_update_store import TelegramUpdateStore


class TelegramUpdateStoreTests(unittest.TestCase):
    def test_mark_processed_rejects_duplicate_update_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TelegramUpdateStore(build_paths(Path(tmp)))

            self.assertTrue(store.mark_processed(101))
            self.assertFalse(store.mark_processed(101))

    def test_processed_update_ids_persist_across_store_instances(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))

            self.assertTrue(TelegramUpdateStore(paths).mark_processed(202))
            self.assertTrue(TelegramUpdateStore(paths).has_processed(202))
            self.assertFalse(TelegramUpdateStore(paths).mark_processed(202))

    def test_store_trims_old_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TelegramUpdateStore(build_paths(Path(tmp)), max_entries=2)

            self.assertTrue(store.mark_processed(1))
            self.assertTrue(store.mark_processed(2))
            self.assertTrue(store.mark_processed(3))

            self.assertFalse(store.has_processed(1))
            self.assertTrue(store.has_processed(2))
            self.assertTrue(store.has_processed(3))


if __name__ == "__main__":
    unittest.main()
