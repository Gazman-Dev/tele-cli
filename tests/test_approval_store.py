from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.paths import build_paths
from runtime.approval_store import ApprovalRecord, ApprovalStore


class ApprovalStoreTests(unittest.TestCase):
    def test_add_and_mark_approval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            store = ApprovalStore(paths)
            store.add(ApprovalRecord(request_id=7, method="approval/request", params={"tool": "shell"}))

            pending = store.get_pending(7)
            self.assertIsNotNone(pending)
            assert pending is not None
            self.assertEqual(pending.method, "approval/request")

            store.mark(7, "approved")
            self.assertIsNone(store.get_pending(7))

    def test_mark_all_pending_stale_only_changes_pending_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            store = ApprovalStore(paths)
            store.add(ApprovalRecord(request_id=7, method="approval/request", params={"tool": "shell"}))
            store.add(ApprovalRecord(request_id=8, method="approval/request", params={"tool": "shell"}, status="approved"))

            changed = store.mark_all_pending_stale()

            self.assertEqual(changed, 1)
            self.assertIsNone(store.get_pending(7))
            stale = store.stale()
            self.assertEqual(len(stale), 1)
            self.assertEqual(stale[0].request_id, 7)


if __name__ == "__main__":
    unittest.main()
