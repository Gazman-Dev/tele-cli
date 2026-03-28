from __future__ import annotations

import select
import sys
import textwrap
import time
import uuid
from dataclasses import dataclass, field

from core.json_store import load_json
from core.models import AuthState, Config, RuntimeState
from core.paths import AppPaths
from demo_ui.state import Colors, visible_len
from demo_ui.ui import TerminalUI
from runtime.app_server_runtime import bootstrap_app_server_session, default_transport_factory
from runtime.instructions import ensure_instruction_files
from runtime.runtime import ServiceRuntime
from runtime.service import (
    default_thinking_text,
    extract_activity_text,
    extract_assistant_text,
    extract_event_driven_status,
    extract_latest_agent_message,
    extract_thinking_delta,
    extract_thinking_text,
    extract_turn_id,
)
from runtime.session_store import SessionStore


def _normalize_session_name(session_name: str | None) -> str:
    candidate = (session_name or "").strip()
    return candidate or "main"


def _wrap_lines(text: str, width: int, prefix: str = "") -> list[str]:
    content = text.strip()
    if not content:
        return []
    wrapped: list[str] = []
    for raw_line in content.splitlines() or [content]:
        if not raw_line:
            wrapped.append(prefix.rstrip())
            continue
        parts = textwrap.wrap(raw_line, width=max(8, width), break_long_words=False, break_on_hyphens=False)
        if not parts:
            wrapped.append(prefix.rstrip())
            continue
        wrapped.append(f"{prefix}{parts[0]}")
        for part in parts[1:]:
            wrapped.append((" " * visible_len(prefix)) + part)
    return wrapped


def _poll_input_key(timeout_seconds: float) -> str | None:
    if sys.platform == "win32":
        import msvcrt

        end_at = time.time() + timeout_seconds
        while time.time() < end_at:
            if not msvcrt.kbhit():
                time.sleep(0.02)
                continue
            char = msvcrt.getwch()
            if char == "\x03":
                raise KeyboardInterrupt
            if char == "\x1b":
                return "esc"
            if char in {"\r", "\n"}:
                return "enter"
            if char in {"\x08", "\x7f"}:
                return "backspace"
            if char in {"\x00", "\xe0"}:
                msvcrt.getwch()
                continue
            if char.isprintable():
                return char
        return None

    fd = sys.stdin.fileno()
    import termios
    import tty

    previous = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        ready, _, _ = select.select([sys.stdin], [], [], timeout_seconds)
        if not ready:
            return None
        char = sys.stdin.read(1)
        if char == "\x03":
            raise KeyboardInterrupt
        if char == "\x1b":
            return "esc"
        if char in {"\r", "\n"}:
            return "enter"
        if char in {"\x7f", "\b"}:
            return "backspace"
        if char.isprintable():
            return char
        return None
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, previous)


@dataclass
class ChatMessage:
    role: str
    text: str


@dataclass
class LocalChatState:
    session_name: str
    input_buffer: str = ""
    assistant_stream: str = ""
    thinking_text: str = ""
    status_text: str = "READY"
    history: list[ChatMessage] = field(default_factory=list)
    notice: str = ""

    def push_message(self, role: str, text: str) -> None:
        stripped = text.strip()
        if not stripped:
            return
        self.history.append(ChatMessage(role=role, text=stripped))
        self.history = self.history[-14:]


class LocalChatApp:
    def __init__(self, paths: AppPaths, session_name: str = "main") -> None:
        self.paths = paths
        self.session_name = _normalize_session_name(session_name)
        self.ui = TerminalUI()
        self.session_store = SessionStore(paths)
        self.state = LocalChatState(session_name=self.session_name)

    def run(self) -> None:
        if not self.ui.is_tty:
            raise SystemExit("Interactive local chat requires a TTY.")
        self.paths.root.mkdir(parents=True, exist_ok=True)
        config = load_json(self.paths.config, Config.from_dict)
        if config is None:
            raise RuntimeError("Run setup first.")
        auth = load_json(self.paths.auth, AuthState.from_dict) or AuthState(bot_token="local-chat")
        ensure_instruction_files(self.paths)
        runtime_state = RuntimeState(
            session_id=str(uuid.uuid4()),
            service_state="LOCAL_CHAT",
            codex_state="STOPPED",
            telegram_state="DISABLED",
            recorder_state="STOPPED",
            debug_state="STOPPED",
        )
        runtime = ServiceRuntime(runtime_state)
        transport = default_transport_factory(config, auth)
        codex = None
        self.ui.begin()
        try:
            codex = bootstrap_app_server_session(
                paths=self.paths,
                auth=auth,
                runtime=runtime,
                runtime_state=runtime_state,
                transport=transport,
                config=config,
            )
            while True:
                self._refresh_status()
                self._render()
                self._drain_notifications(codex)
                key = _poll_input_key(0.05)
                if key is None:
                    continue
                if key == "esc":
                    return
                current = self.session_store.get_current_local_session(self.session_name)
                ready = current is None or not current.active_turn_id
                if not ready:
                    continue
                if key == "backspace":
                    self.state.input_buffer = self.state.input_buffer[:-1]
                    continue
                if key == "enter":
                    text = self.state.input_buffer.strip()
                    self.state.input_buffer = ""
                    if not text:
                        continue
                    if text == "/quit":
                        return
                    if text == "/new":
                        self.session_store.create_new_local_session(self.session_name)
                        self.state.assistant_stream = ""
                        self.state.thinking_text = ""
                        self.state.notice = "Started a fresh local session."
                        continue
                    if text == "/stop":
                        stopped = codex.interrupt_local(self.session_name)
                        self.state.notice = "Stopped the active turn." if stopped else "No active turn to stop."
                        continue
                    self.state.notice = ""
                    self.state.push_message("user", text)
                    codex.send_local(self.session_name, text)
                    self._refresh_status()
                    continue
                if len(key) == 1 and key.isprintable():
                    self.state.input_buffer += key
        finally:
            if codex is not None:
                codex.stop()
            else:
                try:
                    transport.close()
                except Exception:
                    pass
            self.ui.end()

    def _refresh_status(self) -> None:
        session = self.session_store.get_current_local_session(self.session_name)
        ready = session is None or not session.active_turn_id
        self.state.status_text = "READY" if ready else "THINKING"
        if not ready and not self.state.thinking_text:
            self.state.thinking_text = default_thinking_text(session)
        if ready and not self.state.notice:
            self.state.notice = "Type a message. Press Esc or /quit to leave. Use /new for a fresh thread."

    def _render(self) -> None:
        status_badge = (
            f"{Colors.green}{Colors.bold}READY{Colors.reset}"
            if self.state.status_text == "READY"
            else f"{Colors.yellow}{Colors.bold}THINKING{Colors.reset}"
        )
        input_line = self.state.input_buffer or ""
        if self.state.status_text != "READY":
            input_prompt = f"{Colors.muted}Input locked while the assistant is working.{Colors.reset}"
        else:
            input_prompt = f"{Colors.green}{Colors.bold}> {Colors.reset}{input_line}"

        history_lines: list[str] = []
        for message in self.state.history[-8:]:
            label = "You" if message.role == "user" else "Bot" if message.role == "assistant" else "Sys"
            history_lines.extend(_wrap_lines(message.text, 72, prefix=f"{label}: "))
            history_lines.append("")
        if not history_lines:
            history_lines = [f"{Colors.muted}No messages yet in this session.{Colors.reset}"]
        if history_lines and history_lines[-1] == "":
            history_lines.pop()

        thinking_lines = _wrap_lines(
            self.state.thinking_text or "Waiting for work.",
            72,
        ) or [f"{Colors.muted}Waiting for work.{Colors.reset}"]
        stream_lines = _wrap_lines(
            self.state.assistant_stream or "No streamed reply yet.",
            72,
        ) or [f"{Colors.muted}No streamed reply yet.{Colors.reset}"]

        lines = (
            self.ui.print_header()
            + self.ui.panel(
                "Local Session",
                [
                    f"session={self.session_name}",
                    f"status={status_badge}",
                    self.state.notice or "",
                ],
                width=84,
            )
            + [""]
            + self.ui.panel("Conversation", history_lines, width=84)
            + [""]
            + self.ui.panel("Thinking", thinking_lines, width=84)
            + [""]
            + self.ui.panel("Streaming Reply", stream_lines, width=84)
            + [""]
            + self.ui.panel("Input", [input_prompt], width=84)
        )
        self.ui.render(lines)

    def _drain_notifications(self, codex) -> None:
        while True:
            notification = codex.poll_notification()
            if notification is None:
                break
            self._handle_notification(codex, notification.method, notification.params or {})

    def _handle_notification(self, codex, method: str, params: dict) -> None:
        session = self._resolve_session(params)
        if session is None:
            return

        thinking_delta = extract_thinking_delta(method, params)
        if thinking_delta is not None:
            session.thinking_message_text += thinking_delta
            self.state.thinking_text = session.thinking_message_text.strip() or default_thinking_text(session)
            self.session_store.save_session(session)
            return

        text = extract_assistant_text(params)
        thinking_text = extract_thinking_text(params)
        activity_text = extract_activity_text(method, params)
        status_text = extract_event_driven_status(method, params)

        if method in {
            "assistant/message.delta",
            "item/agentMessage/delta",
            "item/updated",
            "item/started",
            "item/completed",
            "turn/output",
            "assistant/message.partial",
        }:
            if text:
                self.session_store.append_pending_output(session, text)
                self.state.assistant_stream = session.pending_output_text.strip()
            elif thinking_text:
                session.thinking_message_text = thinking_text
                self.state.thinking_text = thinking_text
                self.session_store.save_session(session)
            elif activity_text:
                session.thinking_message_text = activity_text
                self.state.thinking_text = activity_text
                self.session_store.save_session(session)
            return

        if status_text:
            session.thinking_message_text = status_text
            self.state.thinking_text = status_text
            self.session_store.save_session(session)

        if method in {"turn/completed", "turn/failed"}:
            turn_id = extract_turn_id(params)
            if not turn_id:
                return
            tracked = self.session_store.find_by_turn_id(str(turn_id))
            if tracked is None:
                tracked = self.session_store.find_by_completed_turn_id(str(turn_id))
            if tracked is None:
                return
            if text:
                self.session_store.append_pending_output(tracked, text)
            elif not tracked.pending_output_text.strip() and tracked.thread_id:
                try:
                    latest = extract_latest_agent_message(codex.read_thread(tracked.thread_id, include_turns=True))
                except Exception:
                    latest = None
                if latest:
                    self.session_store.append_pending_output(tracked, latest)
            final_text = self.session_store.consume_pending_output(tracked).strip()
            tracked.active_turn_id = None
            tracked.last_completed_turn_id = str(turn_id)
            tracked.status = "ACTIVE"
            tracked.thinking_message_text = ""
            self.session_store.save_session(tracked)
            if final_text:
                self.session_store.mark_delivered_output(tracked, final_text)
                self.session_store.mark_agent_message(tracked)
                self.state.push_message("assistant", final_text)
            elif method == "turn/failed":
                self.state.push_message("system", "Turn failed without assistant output.")
            self.state.assistant_stream = ""
            self.state.thinking_text = ""
            self.state.notice = "Ready for the next message."
            return

        if method in {"thread/updated", "thread/resumed"}:
            thread_id = params.get("threadId")
            if isinstance(thread_id, str) and thread_id and not session.thread_id:
                session.thread_id = thread_id
                self.session_store.save_session(session)

    def _resolve_session(self, params: dict):
        turn_id = extract_turn_id(params)
        if turn_id:
            session = self.session_store.find_by_turn_id(str(turn_id))
            if session is not None:
                return session
        thread_id = params.get("threadId")
        if isinstance(thread_id, str):
            session = self.session_store.find_by_thread_id(thread_id)
            if session is not None:
                return session
        return self.session_store.get_current_local_session(self.session_name)


def run_local_chat(paths: AppPaths, session_name: str = "main", channel: str | None = None) -> None:
    LocalChatApp(paths, session_name=channel or session_name).run()
