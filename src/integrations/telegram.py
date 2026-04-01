from __future__ import annotations

import ast
import json
import mimetypes
import queue
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Callable, Optional

from core.models import AuthState, utc_now


class TelegramError(RuntimeError):
    pass


class TelegramClient:
    def __init__(self, token: str):
        self.token = token
        self.base_url = f"https://api.telegram.org/bot{token}"
        self.file_base_url = f"https://api.telegram.org/file/bot{token}"
        self._outbound_queue: queue.Queue[tuple[Callable[[], object], queue.Queue[tuple[bool, object]]]] = queue.Queue()
        self._outbound_worker = threading.Thread(target=self._run_outbound_worker, name="telegram-outbound", daemon=True)
        self._outbound_worker.start()

    @staticmethod
    def _retry_delay_from_error_text(message: str) -> float | None:
        if "429" in message and "Too Many Requests" in message:
            return 1.0
        try:
            payload = ast.literal_eval(message)
        except (ValueError, SyntaxError):
            return None
        if not isinstance(payload, dict) or payload.get("error_code") != 429:
            return None
        parameters = payload.get("parameters")
        if isinstance(parameters, dict):
            retry_after = parameters.get("retry_after")
            if isinstance(retry_after, (int, float)) and retry_after > 0:
                return float(retry_after)
        return 1.0

    @staticmethod
    def _retry_delay_from_error(exc: Exception) -> float | None:
        message = str(exc)
        if "429" in message and "Too Many Requests" in message:
            return 1.0
        if not isinstance(exc, TelegramError):
            return None
        return TelegramClient._retry_delay_from_error_text(message)

    def _run_outbound_worker(self) -> None:
        while True:
            action, result_queue = self._outbound_queue.get()
            attempts = 0
            while True:
                try:
                    result_queue.put((True, action()))
                    break
                except Exception as exc:
                    retry_delay = self._retry_delay_from_error(exc)
                    if retry_delay is None or attempts >= 5:
                        result_queue.put((False, exc))
                        break
                    attempts += 1
                    time.sleep(retry_delay)

    def _dispatch_outbound(self, action: Callable[[], object]):
        result_queue: queue.Queue[tuple[bool, object]] = queue.Queue(maxsize=1)
        self._outbound_queue.put((action, result_queue))
        success, result = result_queue.get()
        if success:
            return result
        raise result

    def _request(
        self,
        method: str,
        params: Optional[dict] = None,
        *,
        data: bytes | None = None,
        headers: Optional[dict[str, str]] = None,
    ) -> dict:
        url = f"{self.base_url}/{method}"
        request_headers = dict(headers or {})
        request: urllib.request.Request | str
        if data is not None:
            request = urllib.request.Request(url, data=data, headers=request_headers, method="POST")
        else:
            if params:
                url = f"{url}?{urllib.parse.urlencode(params)}"
            request = url
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            response_text = ""
            if exc.fp is not None:
                try:
                    response_text = exc.read().decode("utf-8", errors="replace")
                except Exception:
                    response_text = ""
            if response_text:
                try:
                    response_payload = json.loads(response_text)
                except json.JSONDecodeError:
                    raise TelegramError(response_text) from exc
                raise TelegramError(str(response_payload)) from exc
        except urllib.error.URLError as exc:
            raise TelegramError(str(exc)) from exc
        if not payload.get("ok"):
            raise TelegramError(str(payload))
        return payload["result"]

    def _request_bytes(self, url: str) -> bytes:
        try:
            with urllib.request.urlopen(url, timeout=30) as response:
                return response.read()
        except urllib.error.URLError as exc:
            raise TelegramError(str(exc)) from exc

    def _multipart_request(
        self,
        method: str,
        *,
        params: dict[str, object],
        file_field: str,
        file_path: Path,
    ) -> dict:
        boundary = f"tele-cli-{uuid.uuid4().hex}"
        body = bytearray()
        for key, value in params.items():
            if value is None:
                continue
            body.extend(f"--{boundary}\r\n".encode("utf-8"))
            body.extend(f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode("utf-8"))
            body.extend(str(value).encode("utf-8"))
            body.extend(b"\r\n")
        filename = file_path.name
        mime_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        body.extend(f"--{boundary}\r\n".encode("utf-8"))
        body.extend(
            (
                f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'
                f"Content-Type: {mime_type}\r\n\r\n"
            ).encode("utf-8")
        )
        body.extend(file_path.read_bytes())
        body.extend(b"\r\n")
        body.extend(f"--{boundary}--\r\n".encode("utf-8"))
        return self._request(
            method,
            data=bytes(body),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )

    def validate(self) -> dict:
        return self._request("getMe")

    def get_updates(self, offset: Optional[int] = None, timeout: int = 20) -> list[dict]:
        params = {"timeout": timeout}
        if offset is not None:
            params["offset"] = offset
        return self._request("getUpdates", params=params)

    def send_message(
        self,
        chat_id: int,
        text: str,
        topic_id: int | None = None,
        parse_mode: str | None = None,
        disable_notification: bool = False,
    ) -> dict:
        params = {"chat_id": chat_id, "text": text}
        if topic_id is not None:
            params["message_thread_id"] = topic_id
        if parse_mode:
            params["parse_mode"] = parse_mode
        if disable_notification:
            params["disable_notification"] = "true"
        return self._dispatch_outbound(lambda: self._request("sendMessage", params=params))

    def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        parse_mode: str | None = None,
    ) -> dict:
        params = {"chat_id": chat_id, "message_id": message_id, "text": text}
        if parse_mode:
            params["parse_mode"] = parse_mode
        return self._dispatch_outbound(lambda: self._request("editMessageText", params=params))

    def delete_message(self, chat_id: int, message_id: int) -> dict:
        return self._dispatch_outbound(
            lambda: self._request("deleteMessage", params={"chat_id": chat_id, "message_id": message_id})
        )

    def send_photo(
        self,
        chat_id: int,
        photo_path: Path | str,
        *,
        topic_id: int | None = None,
        caption: str | None = None,
        parse_mode: str | None = None,
        disable_notification: bool = False,
    ) -> dict:
        path = Path(photo_path).expanduser().resolve()
        params: dict[str, object] = {"chat_id": chat_id}
        if topic_id is not None:
            params["message_thread_id"] = topic_id
        if caption:
            params["caption"] = caption
        if parse_mode:
            params["parse_mode"] = parse_mode
        if disable_notification:
            params["disable_notification"] = "true"
        return self._dispatch_outbound(
            lambda: self._multipart_request("sendPhoto", params=params, file_field="photo", file_path=path)
        )

    def send_document(
        self,
        chat_id: int,
        document_path: Path | str,
        *,
        topic_id: int | None = None,
        caption: str | None = None,
        parse_mode: str | None = None,
        disable_notification: bool = False,
    ) -> dict:
        path = Path(document_path).expanduser().resolve()
        params: dict[str, object] = {"chat_id": chat_id}
        if topic_id is not None:
            params["message_thread_id"] = topic_id
        if caption:
            params["caption"] = caption
        if parse_mode:
            params["parse_mode"] = parse_mode
        if disable_notification:
            params["disable_notification"] = "true"
        return self._dispatch_outbound(
            lambda: self._multipart_request("sendDocument", params=params, file_field="document", file_path=path)
        )

    def get_file(self, file_id: str) -> dict:
        return self._request("getFile", params={"file_id": file_id})

    def download_file(self, file_path: str) -> bytes:
        encoded_path = "/".join(urllib.parse.quote(segment) for segment in file_path.split("/"))
        return self._request_bytes(f"{self.file_base_url}/{encoded_path}")

    def send_typing(self, chat_id: int, topic_id: int | None = None) -> None:
        params = {"chat_id": chat_id, "action": "typing"}
        if topic_id is not None:
            params["message_thread_id"] = topic_id
        self._dispatch_outbound(lambda: self._request("sendChatAction", params=params))


def is_auth_paired(auth: Optional[AuthState]) -> bool:
    if not auth or not auth.bot_token:
        return False
    return bool(auth.telegram_user_id and auth.paired_at)


def has_pending_pairing(auth: Optional[AuthState]) -> bool:
    if not auth:
        return False
    return bool(auth.pairing_code and auth.pending_chat_id and auth.pending_user_id)


def describe_pairing(auth: Optional[AuthState]) -> str:
    if not auth or not auth.bot_token:
        return "missing bot token"
    if is_auth_paired(auth):
        if auth.telegram_chat_id:
            return f"paired user={auth.telegram_user_id} default_chat={auth.telegram_chat_id}"
        return f"paired user={auth.telegram_user_id}"
    if has_pending_pairing(auth):
        return f"pending code for chat={auth.pending_chat_id} user={auth.pending_user_id}"
    return "not paired"


def issue_pairing_code(auth: AuthState) -> str:
    import random

    auth.pairing_code = f"{random.randint(0, 99999):05d}"
    auth.pending_issued_at = utc_now()
    return auth.pairing_code


def register_pairing_request(auth: AuthState, update: dict) -> tuple[bool, str]:
    message = update.get("message") or {}
    from_user = message.get("from") or {}
    chat = message.get("chat") or {}
    user_id = from_user.get("id")
    chat_id = chat.get("id")
    topic_id = message.get("message_thread_id")
    if is_auth_paired(auth):
        if auth.telegram_user_id != user_id:
            return False, "already-paired"
        return True, "authorized"

    if not user_id or not chat_id:
        return False, "pending"

    if auth.pending_chat_id is None or auth.pending_user_id is None:
        issue_pairing_code(auth)
        auth.pending_chat_id = int(chat_id)
        auth.pending_user_id = int(user_id)
        auth.pending_topic_id = int(topic_id) if isinstance(topic_id, int) else None
        return False, "code-issued"

    if auth.pending_chat_id != int(chat_id) or auth.pending_user_id != int(user_id):
        issue_pairing_code(auth)
        auth.pending_chat_id = int(chat_id)
        auth.pending_user_id = int(user_id)
        auth.pending_topic_id = int(topic_id) if isinstance(topic_id, int) else None
        return False, "code-issued"

    auth.pending_topic_id = int(topic_id) if isinstance(topic_id, int) else None
    return False, "code-issued"


def confirm_pairing_code(auth: AuthState, code: str) -> bool:
    if not auth.pairing_code or not auth.pending_chat_id or not auth.pending_user_id:
        return False
    if code.strip() != auth.pairing_code:
        return False

    auth.telegram_user_id = auth.pending_user_id
    auth.telegram_chat_id = auth.pending_chat_id
    auth.telegram_topic_id = auth.pending_topic_id
    auth.paired_at = utc_now()
    auth.pairing_code = None
    auth.pending_chat_id = None
    auth.pending_user_id = None
    auth.pending_topic_id = None
    auth.pending_issued_at = None
    return True
