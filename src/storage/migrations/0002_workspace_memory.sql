ALTER TABLE sessions ADD COLUMN workspace_id TEXT;
ALTER TABLE sessions ADD COLUMN workspace_kind TEXT;
ALTER TABLE sessions ADD COLUMN workspace_relpath TEXT;
ALTER TABLE sessions ADD COLUMN agents_md_relpath TEXT;
ALTER TABLE sessions ADD COLUMN long_memory_relpath TEXT;
ALTER TABLE sessions ADD COLUMN visible_topic_name TEXT;

CREATE TABLE IF NOT EXISTS workspaces (
    workspace_id TEXT PRIMARY KEY,
    workspace_kind TEXT NOT NULL,
    transport_chat_id INTEGER,
    transport_topic_id INTEGER,
    local_channel TEXT,
    visible_name TEXT,
    relpath TEXT NOT NULL UNIQUE,
    agents_md_relpath TEXT NOT NULL,
    long_memory_relpath TEXT,
    initialized INTEGER NOT NULL DEFAULT 0,
    git_initialized INTEGER NOT NULL DEFAULT 0,
    submodule_initialized INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (workspace_kind IN ('root', 'topic'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_workspaces_root_kind
ON workspaces(workspace_kind)
WHERE workspace_kind = 'root';

CREATE UNIQUE INDEX IF NOT EXISTS idx_workspaces_topic_identity
ON workspaces(transport_chat_id, transport_topic_id)
WHERE workspace_kind = 'topic' AND transport_chat_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_workspaces_local_channel
ON workspaces(local_channel)
WHERE local_channel IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_sessions_workspace_id
ON sessions(workspace_id);
