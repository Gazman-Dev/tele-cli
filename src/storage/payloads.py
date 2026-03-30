from __future__ import annotations

import json
from typing import Any


PREVIEW_LIMIT_BYTES = 1024
GENERAL_PAYLOAD_LIMIT_BYTES = 8192
QUEUE_PAYLOAD_LIMIT_BYTES = 4096
CHUNK_PAYLOAD_LIMIT_BYTES = 4096


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def json_loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    loaded = json.loads(value)
    if loaded is None:
        return default
    return loaded


def truncate_utf8_bytes(text: str, limit_bytes: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= limit_bytes:
        return text
    truncated = encoded[:limit_bytes]
    while True:
        try:
            return truncated.decode("utf-8")
        except UnicodeDecodeError:
            truncated = truncated[:-1]


def preview_text(text: str, *, limit_bytes: int = PREVIEW_LIMIT_BYTES) -> str:
    return truncate_utf8_bytes(text, limit_bytes)
