from __future__ import annotations

import sqlite3
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

    def test_mark_processed_persists_chat_topic_and_payload_preview(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            store = TelegramUpdateStore(paths)

            self.assertTrue(
                store.mark_processed(
                    404,
                    chat_id=22,
                    topic_id=7,
                    payload={"update_id": 404, "message": {"text": "hello"}},
                )
            )

            with sqlite3.connect(paths.database) as connection:
                row = connection.execute(
                    "SELECT chat_id, topic_id, payload_preview, artifact_id FROM telegram_updates WHERE update_id = 404"
                ).fetchone()

            self.assertEqual(row[0], 22)
            self.assertEqual(row[1], 7)
            self.assertIn('"update_id":404', row[2])
            self.assertIsNone(row[3])

    def test_mark_processed_spills_large_payload_to_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            store = TelegramUpdateStore(paths)

            self.assertTrue(
                store.mark_processed(
                    505,
                    payload={"message": {"text": "x" * 9000}},
                )
            )

            with sqlite3.connect(paths.database) as connection:
                row = connection.execute(
                    "SELECT payload_preview, artifact_id FROM telegram_updates WHERE update_id = 505"
                ).fetchone()

            self.assertTrue(row[0])
            self.assertTrue(row[1])

    def test_mark_processed_duplicate_large_payload_does_not_leave_orphan_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            store = TelegramUpdateStore(paths)

            self.assertTrue(store.mark_processed(606, payload={"message": {"text": "x" * 9000}}))
            self.assertFalse(store.mark_processed(606, payload={"message": {"text": "x" * 9000}}))

            with sqlite3.connect(paths.database) as connection:
                artifact_count = connection.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]
                update_row = connection.execute(
                    "SELECT artifact_id FROM telegram_updates WHERE update_id = 606"
                ).fetchone()

            artifact_files = list((paths.artifacts / "telegram_update_payload").glob("*.json"))
            self.assertEqual(artifact_count, 1)
            self.assertEqual(len(artifact_files), 1)
            self.assertIsNotNone(update_row[0])


if __name__ == "__main__":
    unittest.main()
