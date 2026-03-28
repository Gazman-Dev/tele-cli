from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from core.models import AuthState, Config
from core.paths import build_paths
from runtime.instructions import ensure_instruction_files
from runtime.session_store import SessionStore
from runtime.sleep import (
    build_refresh_instructions,
    load_sleep_state,
    run_sleep,
    should_run_sleep,
)
from tests.fakes.fake_app_server import FakeAppServer, InMemoryJsonRpcTransport


class SleepTests(unittest.TestCase):
    def test_ensure_instruction_files_seeds_packaged_defaults_in_state_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))

            with patch("runtime.instructions.detect_repo_root", return_value=paths.root):
                instruction_paths = ensure_instruction_files(paths)

            self.assertEqual(instruction_paths.repo_root, paths.root)
            self.assertTrue(instruction_paths.template.exists())
            self.assertIn("Telegram-first personal assistant", instruction_paths.template.read_text(encoding="utf-8"))
            self.assertIn("best friend", instruction_paths.personality.read_text(encoding="utf-8"))
            self.assertIn("session short memory file", instruction_paths.rules.read_text(encoding="utf-8"))
            self.assertIn("Durable preferences", instruction_paths.long_memory.read_text(encoding="utf-8"))

    def test_sleep_uses_ai_output_clears_short_memory_and_marks_sessions_dirty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            with patch.dict("os.environ", {"TELE_CLI_REPO_ROOT": str(paths.root)}):
                instruction_paths = ensure_instruction_files(paths)
                store = SessionStore(paths)
                auth = AuthState(bot_token="token", telegram_user_id=11, telegram_chat_id=22, paired_at="now")
                session = store.get_or_create_telegram_session(auth)
                session.instructions_dirty = False
                session.last_seen_generation = 0
                store.save_session(session)
                short_memory_path = store.short_memory_path(session.session_id)
                short_memory_path.write_text("- learned a durable preference\n", encoding="utf-8")

                transport = InMemoryJsonRpcTransport()
                server = FakeAppServer(transport)
                server.on("initialize", lambda payload: {"protocolVersion": "1.0", "capabilities": {"threads": True}})
                server.on("getAccount", lambda payload: {"status": "ready"})
                server.on("thread/start", lambda payload: {"threadId": "sleep-thread"})

                def turn_start_handler(payload):
                    server.notify("turn/completed", {"turnId": "sleep-turn"})
                    return {"turnId": "sleep-turn"}

                server.on("turn/start", turn_start_handler)
                server.on(
                    "thread/read",
                    lambda payload: {
                        "thread": {
                            "turns": [
                                {
                                    "items": [
                                        {
                                            "type": "agentMessage",
                                            "text": (
                                                '{"long_memory":"# Long Memory\\n\\n- Durable preference",'
                                                '"lesson":"# Lesson\\n\\n- Learned today"}'
                                            ),
                                        }
                                    ]
                                }
                            ]
                        }
                    },
                )

                run_sleep(
                    paths,
                    Config(state_dir=str(paths.root)),
                    now=datetime(2026, 3, 27, 3, 0, 0).astimezone(),
                    hour_local=2,
                    transport_factory=lambda cfg, auth: transport,
                )

                self.assertEqual(short_memory_path.read_text(encoding="utf-8"), "")
                self.assertIn("Durable preference", instruction_paths.long_memory.read_text(encoding="utf-8"))
                lesson_files = sorted(instruction_paths.lessons_dir.glob("*.md"))
                self.assertEqual(len(lesson_files), 1)
                self.assertIn("Learned today", lesson_files[0].read_text(encoding="utf-8"))
                refreshed = store.get_current_telegram_session(auth)
                self.assertIsNotNone(refreshed)
                assert refreshed is not None
                self.assertTrue(refreshed.instructions_dirty)
                self.assertEqual(load_sleep_state(paths).generation, 1)

    def test_build_refresh_instructions_uses_delta_for_small_missed_lessons(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            with patch.dict("os.environ", {"TELE_CLI_REPO_ROOT": str(paths.root)}):
                instruction_paths = ensure_instruction_files(paths)
                store = SessionStore(paths)
                auth = AuthState(bot_token="token", telegram_user_id=11, telegram_chat_id=22, paired_at="now")
                session = store.get_or_create_telegram_session(auth)
                session.last_seen_generation = 0
                store.save_session(session)
                lesson_file = instruction_paths.lessons_dir / "0001-2026-03-27.md"
                lesson_file.write_text("# Lesson\n\n- Small delta\n", encoding="utf-8")
                from runtime.sleep import save_sleep_state, SleepState

                save_sleep_state(paths, SleepState(generation=1))

                refresh, generation = build_refresh_instructions(paths, session)

                self.assertEqual(generation, 1)
                self.assertIn("Small delta", refresh)
                self.assertIn("missed daily lessons", refresh)

    def test_run_sleep_times_out_when_ai_never_completes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))
            with patch.dict("os.environ", {"TELE_CLI_REPO_ROOT": str(paths.root)}):
                ensure_instruction_files(paths)
                transport = InMemoryJsonRpcTransport()
                server = FakeAppServer(transport)
                server.on("initialize", lambda payload: {"protocolVersion": "1.0", "capabilities": {"threads": True}})
                server.on("getAccount", lambda payload: {"status": "ready"})
                server.on("thread/start", lambda payload: {"threadId": "sleep-thread"})
                server.on("turn/start", lambda payload: {"turnId": "sleep-turn"})

                with self.assertRaisesRegex(RuntimeError, "Sleep AI timed out"):
                    run_sleep(
                        paths,
                        Config(state_dir=str(paths.root)),
                        now=datetime(2026, 3, 27, 3, 0, 0).astimezone(),
                        hour_local=2,
                        transport_factory=lambda cfg, auth: transport,
                        max_wait_seconds=0.01,
                    )

    def test_should_run_sleep_detects_missed_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_paths(Path(tmp))

            self.assertTrue(should_run_sleep(paths, datetime(2026, 3, 27, 9, 0, 0).astimezone(), 2))


if __name__ == "__main__":
    unittest.main()
