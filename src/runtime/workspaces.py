from __future__ import annotations

from dataclasses import dataclass
import re
import subprocess
import uuid
from pathlib import Path

from core.models import SessionRecord, utc_now
from core.paths import AppPaths
from storage.db import StorageManager
from storage.operations import TraceStore


_DEFAULT_GIT_USER_NAME = "Tele Cli"
_DEFAULT_GIT_USER_EMAIL = "tele-cli@example.invalid"
_ROOT_WORKSPACE_RELPATH = "workspace"
_ROOT_AGENTS_RELPATH = "workspace/AGENTS.md"
_ROOT_LONG_MEMORY_RELPATH = "workspace/long_memory.md"
_TOPICS_RELPATH = "workspace/topics"


@dataclass(frozen=True)
class WorkspaceRecord:
    workspace_id: str
    workspace_kind: str
    relpath: str
    agents_md_relpath: str
    long_memory_relpath: str | None = None
    transport_chat_id: int | None = None
    transport_topic_id: int | None = None
    local_channel: str | None = None
    visible_name: str | None = None
    initialized: bool = False
    git_initialized: bool = False
    submodule_initialized: bool = False


def sanitize_workspace_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._ -]+", "-", (value or "").strip())
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .-_")
    return cleaned or "topic"


def default_topic_name(topic_id: int | None) -> str:
    if topic_id is None:
        return "topic"
    return f"topic-{topic_id}"


def workspace_topic_name(session: SessionRecord, visible_name: str | None = None) -> str | None:
    if visible_name and visible_name.strip():
        return visible_name.strip()
    if session.visible_topic_name and session.visible_topic_name.strip():
        return session.visible_topic_name.strip()
    if session.transport == "local":
        channel = (session.transport_channel or "").strip()
        if not channel or channel == "main":
            return None
        parts = [part for part in channel.split("/") if part.strip()]
        return parts[-1] if parts else channel
    if session.transport_topic_id is not None:
        return default_topic_name(session.transport_topic_id)
    return None


class WorkspaceManager:
    def __init__(self, paths: AppPaths):
        self.paths = paths
        self.storage = StorageManager(paths)

    def workspace_path(self, workspace: WorkspaceRecord) -> Path:
        return self.paths.root / Path(workspace.relpath)

    def workspace_path_for_session(self, session: SessionRecord) -> Path:
        bound = self.bind_session(session)
        assert bound.workspace_relpath is not None
        return self.paths.root / Path(bound.workspace_relpath)

    def bind_session(self, session: SessionRecord, visible_topic_name: str | None = None) -> SessionRecord:
        workspace = self.resolve_workspace_for_session(session, visible_topic_name=visible_topic_name)
        session.workspace_id = workspace.workspace_id
        session.workspace_kind = workspace.workspace_kind
        session.workspace_relpath = workspace.relpath
        session.agents_md_relpath = workspace.agents_md_relpath
        session.long_memory_relpath = workspace.long_memory_relpath
        if visible_topic_name and visible_topic_name.strip():
            session.visible_topic_name = visible_topic_name.strip()
        elif workspace.visible_name:
            session.visible_topic_name = workspace.visible_name
        return session

    def resolve_workspace_for_session(self, session: SessionRecord, visible_topic_name: str | None = None) -> WorkspaceRecord:
        if session.transport == "local":
            channel = (session.transport_channel or "main").strip() or "main"
            if channel == "main":
                return self.get_or_create_root_workspace()
            topic_name = workspace_topic_name(session, visible_topic_name) or channel
            return self.get_or_create_topic_workspace(local_channel=channel, visible_name=topic_name)
        if session.transport_topic_id is None:
            return self.get_or_create_root_workspace()
        topic_name = workspace_topic_name(session, visible_topic_name)
        return self.get_or_create_topic_workspace(
            chat_id=session.transport_chat_id,
            topic_id=session.transport_topic_id,
            visible_name=topic_name,
        )

    def ensure_session_workspace(self, session: SessionRecord, visible_topic_name: str | None = None) -> SessionRecord:
        bound = self.bind_session(session, visible_topic_name=visible_topic_name)
        assert bound.workspace_id is not None
        self.ensure_workspace_initialized(bound.workspace_id)
        return bound

    def ensure_workspace_initialized(self, workspace_id: str) -> WorkspaceRecord:
        workspace = self.get_workspace_by_id(workspace_id)
        if workspace is None:
            raise RuntimeError(f"Unknown workspace {workspace_id}.")
        path = self.workspace_path(workspace)
        path.mkdir(parents=True, exist_ok=True)
        if workspace.workspace_kind == "root":
            (path / "topics").mkdir(parents=True, exist_ok=True)
        agents_path = self.paths.root / workspace.agents_md_relpath
        gitignore_path = path / ".gitignore"
        if not agents_path.exists():
            agents_path.write_text(self._render_agent_template(workspace), encoding="utf-8")
        if workspace.long_memory_relpath:
            long_memory_path = self.paths.root / workspace.long_memory_relpath
            if not long_memory_path.exists():
                long_memory_path.write_text("# Long Memory\n\n", encoding="utf-8")
        if not gitignore_path.exists():
            gitignore_path.write_text(self._gitignore_template(workspace), encoding="utf-8")
        git_initialized = self._ensure_git_repo(path, workspace)
        submodule_initialized = workspace.submodule_initialized
        if workspace.workspace_kind == "topic":
            submodule_initialized = self._ensure_root_gitlink_for_topic(workspace)
        updated = WorkspaceRecord(
            workspace_id=workspace.workspace_id,
            workspace_kind=workspace.workspace_kind,
            relpath=workspace.relpath,
            agents_md_relpath=workspace.agents_md_relpath,
            long_memory_relpath=workspace.long_memory_relpath,
            transport_chat_id=workspace.transport_chat_id,
            transport_topic_id=workspace.transport_topic_id,
            local_channel=workspace.local_channel,
            visible_name=workspace.visible_name,
            initialized=True,
            git_initialized=git_initialized,
            submodule_initialized=submodule_initialized,
        )
        self._save_workspace(updated)
        if git_initialized:
            self.best_effort_push_workspace(updated)
        return updated

    def get_workspace_by_id(self, workspace_id: str) -> WorkspaceRecord | None:
        with self.storage.read_connection() as connection:
            row = connection.execute("SELECT * FROM workspaces WHERE workspace_id = ?", (workspace_id,)).fetchone()
        return self._row_to_workspace(row)

    def get_or_create_root_workspace(self) -> WorkspaceRecord:
        with self.storage.transaction() as connection:
            row = connection.execute("SELECT * FROM workspaces WHERE workspace_kind = 'root' LIMIT 1").fetchone()
            if row is not None:
                return self._row_to_workspace(row)
            workspace = WorkspaceRecord(
                workspace_id=str(uuid.uuid4()),
                workspace_kind="root",
                relpath=_ROOT_WORKSPACE_RELPATH,
                agents_md_relpath=_ROOT_AGENTS_RELPATH,
                long_memory_relpath=_ROOT_LONG_MEMORY_RELPATH,
            )
            self._insert_workspace(connection, workspace)
        return workspace

    def get_or_create_topic_workspace(
        self,
        *,
        chat_id: int | None = None,
        topic_id: int | None = None,
        local_channel: str | None = None,
        visible_name: str | None = None,
    ) -> WorkspaceRecord:
        if chat_id is None and local_channel is None:
            raise ValueError("Topic workspace resolution requires a Telegram identity or local channel.")
        with self.storage.transaction() as connection:
            row = None
            if local_channel is not None:
                row = connection.execute("SELECT * FROM workspaces WHERE local_channel = ? LIMIT 1", (local_channel,)).fetchone()
            elif chat_id is not None and topic_id is not None:
                row = connection.execute(
                    """
                    SELECT * FROM workspaces
                    WHERE workspace_kind = 'topic' AND transport_chat_id = ? AND transport_topic_id = ?
                    LIMIT 1
                    """,
                    (chat_id, topic_id),
                ).fetchone()
            if row is not None:
                workspace = self._row_to_workspace(row)
                updated_name = (visible_name or workspace.visible_name or "").strip() or workspace.visible_name
                if updated_name != workspace.visible_name:
                    workspace = WorkspaceRecord(
                        workspace_id=workspace.workspace_id,
                        workspace_kind=workspace.workspace_kind,
                        relpath=workspace.relpath,
                        agents_md_relpath=workspace.agents_md_relpath,
                        long_memory_relpath=workspace.long_memory_relpath,
                        transport_chat_id=workspace.transport_chat_id,
                        transport_topic_id=workspace.transport_topic_id,
                        local_channel=workspace.local_channel,
                        visible_name=updated_name,
                        initialized=workspace.initialized,
                        git_initialized=workspace.git_initialized,
                        submodule_initialized=workspace.submodule_initialized,
                    )
                    self._save_workspace(workspace, connection=connection)
                return workspace

            topic_name = (visible_name or "").strip() or default_topic_name(topic_id)
            relpath = self._allocate_topic_relpath(connection, topic_name, chat_id=chat_id, topic_id=topic_id, local_channel=local_channel)
            workspace = WorkspaceRecord(
                workspace_id=str(uuid.uuid4()),
                workspace_kind="topic",
                relpath=relpath,
                agents_md_relpath=f"{relpath}/AGENTS.md",
                transport_chat_id=chat_id,
                transport_topic_id=topic_id,
                local_channel=local_channel,
                visible_name=topic_name,
            )
            self._insert_workspace(connection, workspace)
        return workspace

    def commit_root_workspace_if_changed(self, message: str) -> bool:
        root_workspace = self.ensure_workspace_initialized(self.get_or_create_root_workspace().workspace_id)
        path = self.workspace_path(root_workspace)
        return self._commit_if_needed(path, message)

    def best_effort_push_workspace(self, workspace: WorkspaceRecord) -> bool:
        path = self.workspace_path(workspace)
        if not (path / ".git").exists():
            return False
        remote = self._git(path, "remote")
        if remote.returncode != 0:
            self._log_workspace_event(
                "workspace.git.remote_probe_failed",
                workspace,
                payload={"stderr": remote.stderr.strip(), "stdout": remote.stdout.strip()},
            )
            return False
        if not remote.stdout.strip():
            return False
        push = self._git(path, "push")
        if push.returncode != 0:
            self._log_workspace_event(
                "workspace.git.push_failed",
                workspace,
                payload={"stderr": push.stderr.strip(), "stdout": push.stdout.strip()},
            )
        else:
            self._log_workspace_event("workspace.git.pushed", workspace)
        return push.returncode == 0

    def _allocate_topic_relpath(
        self,
        connection,
        visible_name: str,
        *,
        chat_id: int | None,
        topic_id: int | None,
        local_channel: str | None,
    ) -> str:
        base_name = sanitize_workspace_name(visible_name)
        suffix_parts = []
        if chat_id is not None and topic_id is not None:
            suffix_parts.append(f"{chat_id}-{topic_id}")
        elif local_channel:
            suffix_parts.append(sanitize_workspace_name(local_channel).replace(" ", "-"))
        suffix = f"-{'-'.join(suffix_parts)}" if suffix_parts else ""
        candidates = [base_name, f"{base_name}{suffix}" if suffix else base_name]
        seen: set[str] = set()
        for candidate in candidates:
            if candidate in seen:
                continue
            seen.add(candidate)
            relpath = f"{_TOPICS_RELPATH}/{candidate}"
            row = connection.execute("SELECT workspace_id FROM workspaces WHERE relpath = ? LIMIT 1", (relpath,)).fetchone()
            if row is None:
                return relpath
        serial = 2
        while True:
            relpath = f"{_TOPICS_RELPATH}/{base_name}{suffix}-{serial}"
            row = connection.execute("SELECT workspace_id FROM workspaces WHERE relpath = ? LIMIT 1", (relpath,)).fetchone()
            if row is None:
                return relpath
            serial += 1

    def _insert_workspace(self, connection, workspace: WorkspaceRecord) -> None:
        now = utc_now()
        connection.execute(
            """
            INSERT INTO workspaces(
                workspace_id, workspace_kind, transport_chat_id, transport_topic_id, local_channel, visible_name,
                relpath, agents_md_relpath, long_memory_relpath, initialized, git_initialized, submodule_initialized,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                workspace.workspace_id,
                workspace.workspace_kind,
                workspace.transport_chat_id,
                workspace.transport_topic_id,
                workspace.local_channel,
                workspace.visible_name,
                workspace.relpath,
                workspace.agents_md_relpath,
                workspace.long_memory_relpath,
                1 if workspace.initialized else 0,
                1 if workspace.git_initialized else 0,
                1 if workspace.submodule_initialized else 0,
                now,
                now,
            ),
        )

    def _save_workspace(self, workspace: WorkspaceRecord, *, connection=None) -> None:
        now = utc_now()
        execute = connection.execute if connection is not None else None
        if execute is None:
            with self.storage.transaction() as tx:
                self._save_workspace(workspace, connection=tx)
            return
        execute(
            """
            UPDATE workspaces
            SET transport_chat_id = ?, transport_topic_id = ?, local_channel = ?, visible_name = ?,
                relpath = ?, agents_md_relpath = ?, long_memory_relpath = ?, initialized = ?, git_initialized = ?,
                submodule_initialized = ?, updated_at = ?
            WHERE workspace_id = ?
            """,
            (
                workspace.transport_chat_id,
                workspace.transport_topic_id,
                workspace.local_channel,
                workspace.visible_name,
                workspace.relpath,
                workspace.agents_md_relpath,
                workspace.long_memory_relpath,
                1 if workspace.initialized else 0,
                1 if workspace.git_initialized else 0,
                1 if workspace.submodule_initialized else 0,
                now,
                workspace.workspace_id,
            ),
        )

    def _row_to_workspace(self, row) -> WorkspaceRecord | None:
        if row is None:
            return None
        return WorkspaceRecord(
            workspace_id=str(row["workspace_id"]),
            workspace_kind=str(row["workspace_kind"]),
            relpath=str(row["relpath"]),
            agents_md_relpath=str(row["agents_md_relpath"]),
            long_memory_relpath=row["long_memory_relpath"],
            transport_chat_id=row["transport_chat_id"],
            transport_topic_id=row["transport_topic_id"],
            local_channel=row["local_channel"],
            visible_name=row["visible_name"],
            initialized=bool(row["initialized"]),
            git_initialized=bool(row["git_initialized"]),
            submodule_initialized=bool(row["submodule_initialized"]),
        )

    def _render_agent_template(self, workspace: WorkspaceRecord) -> str:
        if workspace.workspace_kind == "root":
            return (
                "# AGENTS.md instructions for this workspace\n\n"
                "## Workspace layout\n\n"
                "- This root workspace maps to the direct 1:1 operator chat.\n"
                "- Topic workspaces live under `topics/` and are isolated from each other.\n"
                "- Durable shared memory lives in `long_memory.md` and this `AGENTS.md`.\n"
                "- Temporary Tele Cli memory lives outside this repo under `../memory/`.\n\n"
                "## Git model\n\n"
                "- This directory is the parent workspace repository.\n"
                "- Each topic directory under `topics/` is its own Git repository.\n"
                "- Tele Cli may register topics in the parent repo as submodule-style gitlinks.\n\n"
                "## Constraints\n\n"
                "- Keep changes scoped to the active workspace.\n"
                "- Do not assume memory files under `../memory/` are durable or committed.\n"
            )
        topic_name = workspace.visible_name or default_topic_name(workspace.transport_topic_id)
        return (
            f"# AGENTS.md instructions for {topic_name}\n\n"
            "## Workspace role\n\n"
            "- This directory maps to one Telegram topic workstream.\n"
            "- Treat this workspace as isolated from sibling topics.\n"
            "- Put durable topic guidance in this file.\n\n"
            "## Git model\n\n"
            "- This directory is its own Git repository.\n"
            "- The parent `../..` workspace may track it as a submodule-style entry.\n\n"
            "## Topic details\n\n"
            f"- Visible topic name: `{topic_name}`\n"
            f"- Telegram chat id: `{workspace.transport_chat_id}`\n"
            f"- Telegram topic id: `{workspace.transport_topic_id}`\n"
        )

    def _gitignore_template(self, workspace: WorkspaceRecord) -> str:
        lines = [
            ".DS_Store",
            "*.swp",
            "*.swo",
            "*~",
            "__pycache__/",
            "*.pyc",
            ".idea/",
            ".vscode/",
        ]
        if workspace.workspace_kind == "root":
            lines.append("topics/*/.git")
        return "\n".join(lines) + "\n"

    def _ensure_git_repo(self, path: Path, workspace: WorkspaceRecord) -> bool:
        del workspace
        if not (path / ".git").exists():
            init = self._git(path, "init")
            if init.returncode != 0:
                return False
        self._commit_if_needed(path, "Initialize Tele Cli workspace")
        return True

    def _ensure_root_gitlink_for_topic(self, workspace: WorkspaceRecord) -> bool:
        root_workspace = self.ensure_workspace_initialized(self.get_or_create_root_workspace().workspace_id)
        root_path = self.workspace_path(root_workspace)
        topic_path = self.workspace_path(workspace)
        if not (root_path / ".git").exists() or not (topic_path / ".git").exists():
            return False
        relpath = Path(workspace.relpath).relative_to(_ROOT_WORKSPACE_RELPATH).as_posix()
        head = self._git(topic_path, "rev-parse", "HEAD")
        if head.returncode != 0:
            return False
        module_name = relpath.replace("/", ".")
        self._git(
            root_path,
            "config",
            "-f",
            ".gitmodules",
            f"submodule.{module_name}.path",
            relpath,
        )
        self._git(
            root_path,
            "config",
            "-f",
            ".gitmodules",
            f"submodule.{module_name}.url",
            f"./{relpath}",
        )
        self._git(root_path, "add", ".gitmodules")
        update = self._git(
            root_path,
            "update-index",
            "--add",
            "--cacheinfo",
            f"160000,{head.stdout.strip()},{relpath}",
        )
        if update.returncode != 0:
            return False
        self._commit_if_needed(root_path, f"Track topic workspace {workspace.visible_name or relpath}")
        return True

    def _commit_if_needed(self, path: Path, message: str) -> bool:
        status = self._git(path, "status", "--porcelain")
        if status.returncode != 0 or not status.stdout.strip():
            return False
        self._git(path, "add", "-A")
        commit = self._git(path, "commit", "-m", message)
        return commit.returncode == 0

    def _git(self, cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
        command = [
            "git",
            "-c",
            f"user.name={_DEFAULT_GIT_USER_NAME}",
            "-c",
            f"user.email={_DEFAULT_GIT_USER_EMAIL}",
            *args,
        ]
        return subprocess.run(command, cwd=cwd, text=True, capture_output=True, check=False)

    def _log_workspace_event(
        self,
        event_type: str,
        workspace: WorkspaceRecord,
        *,
        payload: dict[str, str] | None = None,
    ) -> None:
        TraceStore(self.paths).log_event(
            source="workspace",
            event_type=event_type,
            chat_id=workspace.transport_chat_id,
            topic_id=workspace.transport_topic_id,
            payload={
                "workspace_id": workspace.workspace_id,
                "workspace_kind": workspace.workspace_kind,
                "workspace_relpath": workspace.relpath,
                **(payload or {}),
            },
        )
