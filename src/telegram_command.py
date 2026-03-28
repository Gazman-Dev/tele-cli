from __future__ import annotations

from pathlib import Path

from core.json_store import load_json
from core.models import AuthState
from core.paths import AppPaths
from integrations.telegram import TelegramClient, is_auth_paired
from runtime.session_store import SessionStore


def _parse_int_token(value: str, *, prefix: str | None = None) -> int:
    token = value.strip()
    if prefix and token.startswith(prefix):
        token = token[len(prefix) :]
    return int(token)


def resolve_telegram_channel(paths: AppPaths, auth: AuthState, channel: str) -> tuple[int, int | None]:
    normalized = (channel or "main").strip()
    if normalized in {"current", "active"}:
        store = SessionStore(paths)
        sessions = [
            session
            for session in store.load().sessions
            if session.transport == "telegram"
            and session.transport_user_id == auth.telegram_user_id
            and session.attached
            and session.transport_chat_id is not None
        ]
        if not sessions:
            raise SystemExit("No current Telegram session is available.")
        sessions.sort(key=lambda session: (session.last_user_message_at or "", session.last_agent_message_at or ""))
        current = sessions[-1]
        return current.transport_chat_id, current.transport_topic_id
    if normalized == "main":
        if auth.telegram_chat_id is None:
            raise SystemExit("Telegram main channel is not configured.")
        return auth.telegram_chat_id, None
    if "/" in normalized:
        chat_token, topic_token = normalized.split("/", 1)
        return _parse_int_token(chat_token, prefix="chat:"), _parse_int_token(topic_token, prefix="topic:")
    return _parse_int_token(normalized, prefix="chat:"), None


def require_paired_auth(paths: AppPaths) -> AuthState:
    auth = load_json(paths.auth, AuthState.from_dict)
    if not auth or not auth.bot_token:
        raise SystemExit("Telegram bot token is not configured.")
    if not is_auth_paired(auth):
        raise SystemExit("Telegram is not paired yet.")
    return auth


def run_telegram_command(paths: AppPaths, args) -> None:
    auth = require_paired_auth(paths)
    telegram = TelegramClient(auth.bot_token)
    chat_id, topic_id = resolve_telegram_channel(paths, auth, args.channel)
    if args.telegram_target == "message":
        telegram.send_message(chat_id, args.text, topic_id=topic_id)
        return
    path = Path(args.path).expanduser().resolve()
    if not path.exists() or not path.is_file():
        raise SystemExit(f"Telegram attachment path does not exist: {path}")
    if args.telegram_target == "image":
        telegram.send_photo(chat_id, path, topic_id=topic_id, caption=args.caption)
        return
    if args.telegram_target == "file":
        telegram.send_document(chat_id, path, topic_id=topic_id, caption=args.caption)
        return
    raise SystemExit(f"Unsupported telegram target: {args.telegram_target}")
