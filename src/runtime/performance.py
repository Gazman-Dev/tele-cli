from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from core.models import utc_now


class PerformanceTracker:
    def __init__(self, path: Path):
        self.path = path
        self._turns: dict[str, dict[str, Any]] = {}

    def log(self, event: str, **fields: Any) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"timestamp": utc_now(), "event": event, **fields}, sort_keys=True) + "\n")

    def mark_turn_requested(self, session, *, topic_id: int | None, text: str) -> None:
        self._turns[session.session_id] = {
            "requested_at_monotonic": time.monotonic(),
            "reply_started_at_monotonic": None,
            "thread_id": session.thread_id,
            "turn_id": session.active_turn_id,
            "topic_id": topic_id,
        }
        self.log(
            "agent_request_started",
            session_id=session.session_id,
            thread_id=session.thread_id,
            turn_id=session.active_turn_id,
            topic_id=topic_id,
            text_chars=len(text),
        )

    def mark_turn_registered(self, session) -> None:
        entry = self._turns.setdefault(session.session_id, {})
        entry["thread_id"] = session.thread_id
        entry["turn_id"] = session.active_turn_id
        self.log(
            "agent_request_registered",
            session_id=session.session_id,
            thread_id=session.thread_id,
            turn_id=session.active_turn_id,
        )

    def mark_turn_failed(self, session_id: str, *, error: str) -> None:
        self._turns.pop(session_id, None)
        self.log("agent_request_failed", session_id=session_id, error=error)

    def mark_reply_started(self, session, *, trigger: str) -> None:
        entry = self._turns.setdefault(
            session.session_id,
            {
                "requested_at_monotonic": None,
                "reply_started_at_monotonic": None,
                "thread_id": session.thread_id,
                "turn_id": session.active_turn_id,
                "topic_id": session.transport_topic_id,
            },
        )
        if entry.get("reply_started_at_monotonic") is not None:
            return
        now = time.monotonic()
        entry["reply_started_at_monotonic"] = now
        entry["thread_id"] = session.thread_id
        entry["turn_id"] = session.active_turn_id
        requested_at = entry.get("requested_at_monotonic")
        queue_ms = round((now - requested_at) * 1000.0, 1) if requested_at is not None else None
        self.log(
            "agent_reply_started",
            session_id=session.session_id,
            thread_id=session.thread_id,
            turn_id=session.active_turn_id,
            topic_id=session.transport_topic_id,
            trigger=trigger,
            queue_ms=queue_ms,
        )

    def mark_reply_finished(self, session, *, outcome: str) -> None:
        entry = self._turns.pop(session.session_id, {})
        now = time.monotonic()
        requested_at = entry.get("requested_at_monotonic")
        reply_started_at = entry.get("reply_started_at_monotonic")
        total_ms = round((now - requested_at) * 1000.0, 1) if requested_at is not None else None
        reply_ms = round((now - reply_started_at) * 1000.0, 1) if reply_started_at is not None else None
        self.log(
            "agent_reply_finished",
            session_id=session.session_id,
            thread_id=session.thread_id or entry.get("thread_id"),
            turn_id=session.last_completed_turn_id or session.active_turn_id or entry.get("turn_id"),
            topic_id=session.transport_topic_id,
            outcome=outcome,
            total_ms=total_ms,
            reply_ms=reply_ms,
        )


def send_telegram_message(
    telegram,
    chat_id: int,
    text: str,
    *,
    performance: PerformanceTracker | None = None,
    **context: Any,
) -> None:
    started_at = time.monotonic()
    if performance is not None:
        performance.log("telegram_send_started", chat_id=chat_id, text_chars=len(text), **context)
    try:
        telegram.send_message(chat_id, text)
    except Exception as exc:
        if performance is not None:
            performance.log(
                "telegram_send_failed",
                chat_id=chat_id,
                text_chars=len(text),
                duration_ms=round((time.monotonic() - started_at) * 1000.0, 1),
                error=str(exc),
                **context,
            )
        raise
    if performance is not None:
        performance.log(
            "telegram_send_completed",
            chat_id=chat_id,
            text_chars=len(text),
            duration_ms=round((time.monotonic() - started_at) * 1000.0, 1),
            **context,
        )
