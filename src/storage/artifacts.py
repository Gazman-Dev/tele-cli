from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path

from core.models import utc_now
from core.paths import AppPaths

from .db import StorageManager
from .payloads import preview_text


class ArtifactStore:
    def __init__(self, paths: AppPaths):
        self.paths = paths
        self.storage = StorageManager(paths)

    def write_text(self, *, kind: str, text: str, suffix: str = ".txt", connection=None) -> dict[str, object]:
        artifact_id = str(uuid.uuid4())
        directory = self.paths.artifacts / kind
        directory.mkdir(parents=True, exist_ok=True)
        filename = f"{artifact_id}{suffix}"
        path = directory / filename
        path.write_text(text, encoding="utf-8")
        relpath = path.relative_to(self.paths.root).as_posix()
        payload = path.read_bytes()
        sha256 = hashlib.sha256(payload).hexdigest()
        if connection is not None:
            connection.execute(
                """
                INSERT INTO artifacts(artifact_id, kind, relpath, size_bytes, sha256, created_at, expires_at, compressed)
                VALUES (?, ?, ?, ?, ?, ?, NULL, 0)
                """,
                (artifact_id, kind, relpath, len(payload), sha256, utc_now()),
            )
        else:
            with self.storage.transaction() as tx:
                tx.execute(
                    """
                    INSERT INTO artifacts(artifact_id, kind, relpath, size_bytes, sha256, created_at, expires_at, compressed)
                    VALUES (?, ?, ?, ?, ?, ?, NULL, 0)
                    """,
                    (artifact_id, kind, relpath, len(payload), sha256, utc_now()),
                )
        return {
            "storage": "artifact",
            "artifact_id": artifact_id,
            "kind": kind,
            "relpath": relpath,
            "size_bytes": len(payload),
            "preview": preview_text(text),
        }

    def write_json(self, *, kind: str, value: object, suffix: str = ".json", connection=None) -> dict[str, object]:
        return self.write_text(
            kind=kind,
            text=json.dumps(value, ensure_ascii=False, sort_keys=True),
            suffix=suffix,
            connection=connection,
        )

    def delete(self, reference: dict[str, object], *, connection=None) -> None:
        if not self.is_reference(reference):
            return
        artifact_id = reference.get("artifact_id")
        relpath = reference.get("relpath")
        if isinstance(relpath, str) and relpath:
            path = self.paths.root / Path(relpath)
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        if not isinstance(artifact_id, str) or not artifact_id:
            return
        if connection is not None:
            connection.execute("DELETE FROM artifacts WHERE artifact_id = ?", (artifact_id,))
            return
        with self.storage.transaction() as tx:
            tx.execute("DELETE FROM artifacts WHERE artifact_id = ?", (artifact_id,))

    @staticmethod
    def is_reference(value: object) -> bool:
        return (
            isinstance(value, dict)
            and value.get("storage") == "artifact"
            and isinstance(value.get("artifact_id"), str)
            and bool(value.get("artifact_id"))
        )

    def read_text(self, reference: dict[str, object]) -> str:
        relpath = reference.get("relpath")
        if not isinstance(relpath, str) or not relpath:
            raise ValueError("Artifact reference is missing relpath.")
        return self.paths.root.joinpath(relpath).read_text(encoding="utf-8")

    def read_json(self, reference: dict[str, object], default: object) -> object:
        if not self.is_reference(reference):
            return default
        return json.loads(self.read_text(reference))
