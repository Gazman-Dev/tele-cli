from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.models import AuthState
from core.paths import build_paths
from runtime.session_store import SessionStore


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


if __name__ == "__main__":
    unittest.main()
