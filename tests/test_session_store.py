from __future__ import annotations

from contextlib import closing
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.models import AuthState, SessionRecord
from core.paths import build_paths
from runtime.session_store import SessionStore
from runtime.workspaces import WorkspaceManager


class SessionStoreTests(unittest.TestCase):
    def test_get_or_create_telegram_session_reuses_same_chat_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            store = SessionStore(paths)
            auth = AuthState(bot_token="token", telegram_user_id=11, telegram_chat_id=22, paired_at="now")

            first = store.get_or_create_telegram_session(auth)
            second = store.get_or_create_telegram_session(auth)

            self.assertEqual(first.session_id, second.session_id)

    def test_get_or_create_telegram_session_separates_topics_within_same_chat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            store = SessionStore(paths)
            auth = AuthState(bot_token="token", telegram_user_id=11, telegram_chat_id=22, paired_at="now")

            first = store.get_or_create_telegram_session(auth, topic_id=101)
            second = store.get_or_create_telegram_session(auth, topic_id=202)

            self.assertNotEqual(first.session_id, second.session_id)
            self.assertEqual(first.transport_topic_id, 101)
            self.assertEqual(second.transport_topic_id, 202)

    def test_save_session_persists_thread_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            store = SessionStore(paths)
            auth = AuthState(bot_token="token", telegram_user_id=11, telegram_chat_id=22, paired_at="now")

            session = store.get_or_create_telegram_session(auth)
            session.thread_id = "thread-1"
            store.save_session(session)

            loaded = store.get_or_create_telegram_session(auth)
            self.assertEqual(loaded.thread_id, "thread-1")

    def test_save_session_preserves_existing_thread_id_for_active_turn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            store = SessionStore(paths)
            auth = AuthState(bot_token="token", telegram_user_id=11, telegram_chat_id=22, paired_at="now")

            session = store.get_or_create_telegram_session(auth)
            session.thread_id = "thread-1"
            session.active_turn_id = "turn-1"
            session.status = "RUNNING_TURN"
            store.save_session(session)

            broken = SessionRecord.from_dict(session.to_dict())
            broken.thread_id = None
            store.save_session(broken)

            loaded = store.get_or_create_telegram_session(auth)
            self.assertEqual(loaded.thread_id, "thread-1")
            self.assertEqual(loaded.active_turn_id, "turn-1")

    def test_get_or_create_telegram_session_prefers_active_session_after_new(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            store = SessionStore(paths)
            auth = AuthState(bot_token="token", telegram_user_id=11, telegram_chat_id=22, paired_at="now")

            original = store.get_or_create_telegram_session(auth)
            new_session = store.create_new_telegram_session(auth)
            loaded = store.get_or_create_telegram_session(auth)

            self.assertNotEqual(original.session_id, new_session.session_id)
            self.assertEqual(loaded.session_id, new_session.session_id)

    def test_create_new_telegram_session_detaches_previous_session_and_keeps_turn_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            store = SessionStore(paths)
            auth = AuthState(bot_token="token", telegram_user_id=11, telegram_chat_id=22, paired_at="now")

            original = store.get_or_create_telegram_session(auth)
            original.active_turn_id = "turn-1"
            original.thread_id = "thread-1"
            original.pending_output_text = "partial"
            original.status = "RUNNING_TURN"
            store.save_session(original)

            store.create_new_telegram_session(auth)
            sessions = store.list_telegram_sessions(auth)
            archived = next(session for session in sessions if session.session_id == original.session_id)

            self.assertFalse(archived.attached)
            self.assertEqual(archived.status, "RUNNING_TURN")
            self.assertEqual(archived.active_turn_id, "turn-1")
            self.assertEqual(archived.pending_output_text, "partial")
            self.assertEqual(archived.thread_id, "thread-1")

    def test_create_new_telegram_session_only_detaches_sessions_in_same_topic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            store = SessionStore(paths)
            auth = AuthState(bot_token="token", telegram_user_id=11, telegram_chat_id=22, paired_at="now")

            topic_a = store.get_or_create_telegram_session(auth, topic_id=101)
            topic_b = store.get_or_create_telegram_session(auth, topic_id=202)
            replacement = store.create_new_telegram_session(auth, topic_id=101)

            sessions_a = store.list_telegram_sessions(auth, 101)
            sessions_b = store.list_telegram_sessions(auth, 202)

            self.assertFalse(any(session.session_id == topic_a.session_id for session in sessions_a))
            current_a = next(session for session in sessions_a if session.session_id == replacement.session_id)
            self.assertTrue(current_a.attached)
            self.assertEqual(len(sessions_b), 1)
            self.assertEqual(sessions_b[0].session_id, topic_b.session_id)
            self.assertTrue(sessions_b[0].attached)

    def test_find_by_completed_turn_id_returns_completed_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            store = SessionStore(paths)
            auth = AuthState(bot_token="token", telegram_user_id=11, telegram_chat_id=22, paired_at="now")

            session = store.get_or_create_telegram_session(auth)
            session.last_completed_turn_id = "turn-9"
            store.save_session(session)

            loaded = store.find_by_completed_turn_id("turn-9")

            self.assertIsNotNone(loaded)
            assert loaded is not None
            self.assertEqual(loaded.session_id, session.session_id)

    def test_short_memory_path_is_shared_state_file_per_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            store = SessionStore(paths)
            auth = AuthState(bot_token="token", telegram_user_id=11, telegram_chat_id=22, paired_at="now")
            session = store.get_or_create_telegram_session(auth)

            path = store.short_memory_path(session.session_id)

            self.assertEqual(path, paths.root / "memory" / "sessions" / f"{session.session_id}.short_memory.md")
            self.assertTrue(path.exists())

    def test_create_new_prunes_already_idle_detached_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            store = SessionStore(paths)
            auth = AuthState(bot_token="token", telegram_user_id=11, telegram_chat_id=22, paired_at="now")

            store.get_or_create_telegram_session(auth)
            second = store.create_new_telegram_session(auth)
            second.active_turn_id = "turn-2"
            second.pending_output_text = "working"
            second.status = "RUNNING_TURN"
            store.save_session(second)

            third = store.create_new_telegram_session(auth)
            sessions = store.list_telegram_sessions(auth)

            self.assertEqual(len(sessions), 2)
            self.assertTrue(any(session.session_id == second.session_id for session in sessions))
            self.assertTrue(any(session.session_id == third.session_id for session in sessions))

    def test_get_or_create_local_session_reuses_same_channel(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            store = SessionStore(paths)

            first = store.get_or_create_local_session("main")
            second = store.get_or_create_local_session("main")

            self.assertEqual(first.session_id, second.session_id)
            self.assertEqual(first.transport_channel, "main")

    def test_get_or_create_local_session_separates_channels(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            store = SessionStore(paths)

            first = store.get_or_create_local_session("main")
            second = store.get_or_create_local_session("my_group/topic1")

            self.assertNotEqual(first.session_id, second.session_id)
            self.assertEqual(second.transport_channel, "my_group/topic1")

    def test_create_new_local_session_detaches_previous_channel_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            store = SessionStore(paths)

            original = store.get_or_create_local_session("main")
            original.active_turn_id = "turn-1"
            original.status = "RUNNING_TURN"
            store.save_session(original)

            replacement = store.create_new_local_session("main")
            sessions = store.list_local_sessions("main")

            self.assertEqual(len(sessions), 1)
            self.assertEqual(sessions[0].session_id, replacement.session_id)
            self.assertEqual(replacement.transport_channel, "main")

    def test_root_session_is_bound_to_workspace_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            store = SessionStore(paths)
            auth = AuthState(bot_token="token", telegram_user_id=11, telegram_chat_id=22, paired_at="now")

            session = store.get_or_create_telegram_session(auth)

            self.assertEqual(session.workspace_kind, "root")
            self.assertEqual(session.workspace_relpath, "workspace")
            self.assertEqual(session.agents_md_relpath, "workspace/AGENTS.md")
            self.assertEqual(session.long_memory_relpath, "workspace/long_memory.md")

    def test_topic_workspace_name_is_stable_across_visible_rename(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            manager = WorkspaceManager(paths)

            original = manager.get_or_create_topic_workspace(chat_id=22, topic_id=101, visible_name="Bayonne pump")
            renamed = manager.get_or_create_topic_workspace(chat_id=22, topic_id=101, visible_name="Bayonne pump urgent")

            self.assertEqual(original.workspace_id, renamed.workspace_id)
            self.assertEqual(original.relpath, renamed.relpath)
            self.assertEqual(renamed.visible_name, "Bayonne pump urgent")
            self.assertIn("Bayonne pump", original.relpath)

    def test_workspace_scaffolding_creates_agent_long_memory_and_git_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            manager = WorkspaceManager(paths)

            root = manager.ensure_workspace_initialized(manager.get_or_create_root_workspace().workspace_id)
            topic = manager.ensure_workspace_initialized(
                manager.get_or_create_topic_workspace(chat_id=22, topic_id=101, visible_name="Bayonne pump").workspace_id
            )

            self.assertTrue((paths.root / root.agents_md_relpath).exists())
            self.assertTrue((paths.root / root.long_memory_relpath).exists())
            self.assertTrue((paths.root / root.relpath / ".git").exists())
            self.assertTrue((paths.root / topic.agents_md_relpath).exists())
            self.assertTrue((paths.root / topic.relpath / ".git").exists())

    def test_workspace_push_failure_is_logged_when_remote_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            manager = WorkspaceManager(paths)
            workspace = manager.ensure_workspace_initialized(manager.get_or_create_root_workspace().workspace_id)

            def fake_git(_cwd, *args):
                if args == ("remote",):
                    return unittest.mock.Mock(returncode=0, stdout="origin\n", stderr="")
                if args == ("push",):
                    return unittest.mock.Mock(returncode=1, stdout="", stderr="push rejected")
                return original_git(_cwd, *args)

            original_git = manager._git
            with patch.object(manager, "_git", side_effect=fake_git):
                self.assertFalse(manager.best_effort_push_workspace(workspace))

            with closing(sqlite3.connect(paths.database)) as connection:
                row = connection.execute(
                    """
                    SELECT event_type, payload_preview
                    FROM events
                    WHERE source = 'workspace'
                    ORDER BY event_id DESC
                    LIMIT 1
                    """
                ).fetchone()

            self.assertIsNotNone(row)
            assert row is not None
            self.assertEqual(row[0], "workspace.git.push_failed")
            self.assertIn("push rejected", row[1])


if __name__ == "__main__":
    unittest.main()
