from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from core.models import utc_now
from storage.telegram_queue import active_delivery_manager


class PerformanceTracker:
    def __init__(self, path: Path):
        self.path = path
        self._turns: dict[str, dict[str, Any]] = {}

    def log(self, event: str, **fields: Any) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"timestamp": utc_now(), "event": event, **fields}, sort_keys=True) + "\n")

    def mark_notification_received(self, method: str, params: dict[str, Any]) -> None:
        item = params.get("item") if isinstance(params.get("item"), dict) else {}
        snippet = None
        for candidate in (
            params.get("delta"),
            params.get("text"),
            params.get("outputText"),
            params.get("reasoning"),
            params.get("summary"),
            item.get("text"),
        ):
            if isinstance(candidate, str) and candidate:
                snippet = candidate[:160]
                break
        self.log(
            "codex_notification_received",
            method=method,
            turn_id=params.get("turnId") or params.get("turn_id"),
            thread_id=params.get("threadId") or params.get("thread_id"),
            item_type=item.get("type"),
            keys=sorted(str(key) for key in params.keys()),
            text_excerpt=snippet,
        )

    def mark_telegram_message_received(
        self,
        *,
        update_id: int | None,
        chat_id: int | None,
        topic_id: int | None,
        text: str,
    ) -> None:
        self.log(
            "telegram_message_received",
            update_id=update_id,
            chat_id=chat_id,
            topic_id=topic_id,
            text_chars=len(text),
        )

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

    def mark_ai_dispatch_started(self, session) -> None:
        entry = self._turns.setdefault(session.session_id, {})
        entry["thread_id"] = session.thread_id
        entry["turn_id"] = session.active_turn_id
        self.log(
            "ai_dispatch_started",
            session_id=session.session_id,
            thread_id=session.thread_id,
            turn_id=session.active_turn_id,
            topic_id=session.transport_topic_id,
        )

    def mark_thread_ready(self, session, *, trigger: str) -> None:
        entry = self._turns.setdefault(session.session_id, {})
        entry["thread_id"] = session.thread_id
        self.log(
            "ai_thread_ready",
            session_id=session.session_id,
            thread_id=session.thread_id,
            turn_id=session.active_turn_id,
            topic_id=session.transport_topic_id,
            trigger=trigger,
        )

    def mark_turn_registered(self, session) -> None:
        entry = self._turns.setdefault(session.session_id, {})
        entry["thread_id"] = session.thread_id
        entry["turn_id"] = session.active_turn_id
        self.log(
            "ai_turn_acknowledged",
            session_id=session.session_id,
            thread_id=session.thread_id,
            turn_id=session.active_turn_id,
            topic_id=session.transport_topic_id,
        )

    def mark_turn_failed(self, session_id: str, *, error: str) -> None:
        self._turns.pop(session_id, None)
        self.log("agent_request_failed", session_id=session_id, error=error)

    def mark_reply_started(self, session, *, trigger: str) -> bool:
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
            return False
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
        return True

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


def _require_delivery_manager():
    manager = active_delivery_manager()
    if manager is None:
        raise RuntimeError("Telegram delivery manager is not installed.")
    return manager


def send_telegram_message(
    telegram,
    chat_id: int,
    text: str,
    *,
    topic_id: int | None = None,
    parse_mode: str | None = None,
    disable_notification: bool = False,
    allow_plain_fallback: bool = False,
    plain_fallback_text: str | None = None,
    fallback_parse_mode: str | None = None,
    allow_paused_return: bool = False,
    performance: PerformanceTracker | None = None,
    **context: Any,
) -> int | None:
    started_at = time.monotonic()
    if performance is not None:
        performance.log(
            "telegram_send_started",
            chat_id=chat_id,
            topic_id=topic_id,
            text_chars=len(text),
            parse_mode=parse_mode,
            disable_notification=disable_notification,
            **context,
        )
    try:
        manager = _require_delivery_manager()
        result = manager.enqueue_and_wait(
            op_type="send_message",
            payload={"text": text, "parse_mode": parse_mode},
            allow_paused_return=allow_paused_return,
            chat_id=chat_id,
            topic_id=topic_id,
            session_id=context.get("session_id"),
            trace_id=context.get("trace_id"),
            message_group_id=context.get("message_group_id"),
            dedupe_key=context.get("dedupe_key"),
            priority=int(context.get("priority", 100)),
            disable_notification=disable_notification,
        )
    except Exception as exc:
        if allow_plain_fallback and parse_mode:
            fallback_text = plain_fallback_text if plain_fallback_text is not None else text
            if performance is not None:
                performance.log(
                    "telegram_send_retry_plain",
                chat_id=chat_id,
                topic_id=topic_id,
                text_chars=len(fallback_text),
                parse_mode=parse_mode,
                fallback_parse_mode=fallback_parse_mode,
                disable_notification=disable_notification,
                error=str(exc),
                **context,
            )
            return send_telegram_message(
                telegram,
                chat_id,
                fallback_text,
                topic_id=topic_id,
                parse_mode=fallback_parse_mode,
                allow_plain_fallback=False,
                allow_paused_return=allow_paused_return,
                performance=performance,
                **context,
            )
        if performance is not None:
            performance.log(
                "telegram_send_failed",
                chat_id=chat_id,
                topic_id=topic_id,
                text_chars=len(text),
                parse_mode=parse_mode,
                disable_notification=disable_notification,
                duration_ms=round((time.monotonic() - started_at) * 1000.0, 1),
                error=str(exc),
                **context,
            )
        raise
    if performance is not None:
        performance.log(
            "telegram_send_completed",
            chat_id=chat_id,
            topic_id=topic_id,
            text_chars=len(text),
            parse_mode=parse_mode,
            disable_notification=disable_notification,
            duration_ms=round((time.monotonic() - started_at) * 1000.0, 1),
            **context,
        )
    if isinstance(result, dict):
        message_id = result.get("message_id")
        if isinstance(message_id, int):
            return message_id
    return None


def edit_telegram_message(
    telegram,
    chat_id: int,
    message_id: int,
    text: str,
    *,
    parse_mode: str | None = None,
    allow_plain_fallback: bool = False,
    plain_fallback_text: str | None = None,
    fallback_parse_mode: str | None = None,
    allow_paused_return: bool = False,
    performance: PerformanceTracker | None = None,
    **context: Any,
) -> None:
    started_at = time.monotonic()
    if performance is not None:
        performance.log(
            "telegram_edit_started",
            chat_id=chat_id,
            message_id=message_id,
            text_chars=len(text),
            parse_mode=parse_mode,
            **context,
        )
    try:
        manager = _require_delivery_manager()
        manager.enqueue_and_wait(
            op_type="edit_message",
            payload={"message_id": message_id, "text": text, "parse_mode": parse_mode},
            allow_paused_return=allow_paused_return,
            chat_id=chat_id,
            session_id=context.get("session_id"),
            trace_id=context.get("trace_id"),
            message_group_id=context.get("message_group_id"),
            telegram_message_id=message_id,
            dedupe_key=context.get("dedupe_key"),
            priority=int(context.get("priority", 100)),
        )
    except Exception as exc:
        if allow_plain_fallback and parse_mode:
            fallback_text = plain_fallback_text if plain_fallback_text is not None else text
            if performance is not None:
                performance.log(
                    "telegram_edit_retry_plain",
                    chat_id=chat_id,
                    message_id=message_id,
                    text_chars=len(fallback_text),
                    parse_mode=parse_mode,
                    fallback_parse_mode=fallback_parse_mode,
                    error=str(exc),
                    **context,
                )
            edit_telegram_message(
                telegram,
                chat_id,
                message_id,
                fallback_text,
                parse_mode=fallback_parse_mode,
                allow_plain_fallback=False,
                allow_paused_return=allow_paused_return,
                performance=performance,
                **context,
            )
            return
        if performance is not None:
            performance.log(
                "telegram_edit_failed",
                chat_id=chat_id,
                message_id=message_id,
                text_chars=len(text),
                parse_mode=parse_mode,
                duration_ms=round((time.monotonic() - started_at) * 1000.0, 1),
                error=str(exc),
                **context,
            )
        raise
    if performance is not None:
        performance.log(
            "telegram_edit_completed",
            chat_id=chat_id,
            message_id=message_id,
            text_chars=len(text),
            parse_mode=parse_mode,
            duration_ms=round((time.monotonic() - started_at) * 1000.0, 1),
            **context,
        )


def delete_telegram_message(
    telegram,
    chat_id: int,
    message_id: int,
    *,
    allow_paused_return: bool = False,
    performance: PerformanceTracker | None = None,
    **context: Any,
) -> None:
    manager = _require_delivery_manager()
    if performance is not None:
        performance.log("telegram_delete_started", chat_id=chat_id, message_id=message_id, **context)
    started_at = time.monotonic()
    manager.enqueue_and_wait(
        op_type="delete_message",
        payload={"message_id": message_id},
        allow_paused_return=allow_paused_return,
        chat_id=chat_id,
        session_id=context.get("session_id"),
        trace_id=context.get("trace_id"),
        message_group_id=context.get("message_group_id"),
        telegram_message_id=message_id,
        priority=int(context.get("priority", 100)),
    )
    if performance is not None:
        performance.log(
            "telegram_delete_completed",
            chat_id=chat_id,
            message_id=message_id,
            duration_ms=round((time.monotonic() - started_at) * 1000.0, 1),
            **context,
        )


def send_telegram_typing(
    telegram,
    chat_id: int,
    *,
    topic_id: int | None = None,
    allow_paused_return: bool = False,
    performance: PerformanceTracker | None = None,
    **context: Any,
) -> None:
    manager = _require_delivery_manager()
    manager.enqueue_and_wait(
        op_type="typing",
        payload={},
        allow_paused_return=allow_paused_return,
        chat_id=chat_id,
        topic_id=topic_id,
        session_id=context.get("session_id"),
        trace_id=context.get("trace_id"),
        priority=int(context.get("priority", 200)),
    )
