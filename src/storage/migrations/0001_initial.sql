CREATE TABLE IF NOT EXISTS service_runs (
    run_id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    stopped_at TEXT,
    version TEXT,
    pid INTEGER,
    hostname TEXT,
    state_dir TEXT,
    exit_reason TEXT
);

CREATE INDEX IF NOT EXISTS idx_service_runs_started_at
ON service_runs(started_at);

CREATE TABLE IF NOT EXISTS app_state (
    state_key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    transport TEXT NOT NULL,
    transport_user_id INTEGER,
    transport_chat_id INTEGER,
    transport_topic_id INTEGER,
    transport_channel TEXT,
    attached INTEGER NOT NULL,
    status TEXT NOT NULL,
    thread_id TEXT,
    active_turn_id TEXT,
    last_completed_turn_id TEXT,
    current_trace_id TEXT,
    instructions_dirty INTEGER NOT NULL,
    last_seen_generation INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    last_user_message_at TEXT,
    last_agent_message_at TEXT,
    streaming_message_id INTEGER,
    streaming_message_ids_json TEXT NOT NULL DEFAULT '[]',
    thinking_message_id INTEGER,
    thinking_message_ids_json TEXT NOT NULL DEFAULT '[]',
    thinking_live_message_ids_json TEXT NOT NULL DEFAULT '{}',
    thinking_live_texts_json TEXT NOT NULL DEFAULT '{}',
    thinking_sent_texts_json TEXT NOT NULL DEFAULT '{}',
    thinking_history_order_json TEXT NOT NULL DEFAULT '[]',
    thinking_history_by_source_json TEXT NOT NULL DEFAULT '{}',
    streaming_output_text TEXT NOT NULL DEFAULT '',
    streaming_phase TEXT NOT NULL DEFAULT '',
    thinking_message_text TEXT NOT NULL DEFAULT '',
    thinking_history_text TEXT NOT NULL DEFAULT '',
    last_thinking_sent_text TEXT NOT NULL DEFAULT '',
    pending_output_text TEXT NOT NULL DEFAULT '',
    queued_user_input_text TEXT NOT NULL DEFAULT '',
    pending_output_updated_at TEXT,
    last_delivered_output_text TEXT NOT NULL DEFAULT '',
    CHECK (active_turn_id IS NULL OR thread_id IS NOT NULL)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_attached_telegram
ON sessions(transport, transport_chat_id, transport_topic_id)
WHERE attached = 1 AND transport = 'telegram';

CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_attached_local
ON sessions(transport, transport_channel)
WHERE attached = 1 AND transport = 'local';

CREATE INDEX IF NOT EXISTS idx_sessions_transport_chat_topic
ON sessions(transport, transport_chat_id, transport_topic_id);

CREATE INDEX IF NOT EXISTS idx_sessions_transport_channel
ON sessions(transport, transport_channel);

CREATE INDEX IF NOT EXISTS idx_sessions_thread_id
ON sessions(thread_id);

CREATE INDEX IF NOT EXISTS idx_sessions_active_turn_id
ON sessions(active_turn_id);

CREATE INDEX IF NOT EXISTS idx_sessions_last_completed_turn_id
ON sessions(last_completed_turn_id);

CREATE TABLE IF NOT EXISTS session_short_memory (
    session_id TEXT PRIMARY KEY REFERENCES sessions(session_id) ON DELETE CASCADE,
    short_memory_relpath TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS telegram_updates (
    update_id INTEGER PRIMARY KEY,
    chat_id INTEGER,
    topic_id INTEGER,
    received_at TEXT NOT NULL,
    processed_at TEXT,
    status TEXT NOT NULL,
    payload_preview TEXT,
    artifact_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_telegram_updates_chat_topic
ON telegram_updates(chat_id, topic_id);

CREATE INDEX IF NOT EXISTS idx_telegram_updates_processed_at
ON telegram_updates(processed_at);

CREATE TABLE IF NOT EXISTS traces (
    trace_id TEXT PRIMARY KEY,
    session_id TEXT REFERENCES sessions(session_id),
    thread_id TEXT,
    turn_id TEXT,
    parent_trace_id TEXT REFERENCES traces(trace_id),
    chat_id INTEGER,
    topic_id INTEGER,
    user_text_preview TEXT,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    outcome TEXT
);

CREATE INDEX IF NOT EXISTS idx_traces_session_id
ON traces(session_id);

CREATE INDEX IF NOT EXISTS idx_traces_thread_turn
ON traces(thread_id, turn_id);

CREATE INDEX IF NOT EXISTS idx_traces_started_at
ON traces(started_at);

CREATE TABLE IF NOT EXISTS approvals (
    request_id INTEGER PRIMARY KEY,
    session_id TEXT REFERENCES sessions(session_id),
    thread_id TEXT,
    turn_id TEXT,
    trace_id TEXT REFERENCES traces(trace_id),
    method TEXT NOT NULL,
    params_json TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    resolved_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_approvals_status
ON approvals(status);

CREATE INDEX IF NOT EXISTS idx_approvals_session_id
ON approvals(session_id);

CREATE TABLE IF NOT EXISTS events (
    event_id INTEGER PRIMARY KEY,
    trace_id TEXT REFERENCES traces(trace_id),
    run_id TEXT REFERENCES service_runs(run_id),
    source TEXT NOT NULL,
    event_type TEXT NOT NULL,
    received_at TEXT NOT NULL,
    handled_at TEXT,
    session_id TEXT REFERENCES sessions(session_id),
    thread_id TEXT,
    turn_id TEXT,
    item_id TEXT,
    source_event_id TEXT,
    chat_id INTEGER,
    topic_id INTEGER,
    message_group_id TEXT,
    telegram_message_id INTEGER,
    payload_json TEXT,
    payload_preview TEXT,
    artifact_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_events_trace_id
ON events(trace_id);

CREATE INDEX IF NOT EXISTS idx_events_thread_turn
ON events(thread_id, turn_id);

CREATE INDEX IF NOT EXISTS idx_events_session_id
ON events(session_id);

CREATE INDEX IF NOT EXISTS idx_events_received_at
ON events(received_at);

CREATE INDEX IF NOT EXISTS idx_events_source_type
ON events(source, event_type);

CREATE TABLE IF NOT EXISTS telegram_outbound_queue (
    queue_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    available_at TEXT NOT NULL,
    status TEXT NOT NULL,
    op_type TEXT NOT NULL,
    chat_id INTEGER NOT NULL,
    topic_id INTEGER,
    session_id TEXT REFERENCES sessions(session_id),
    trace_id TEXT REFERENCES traces(trace_id),
    message_group_id TEXT,
    telegram_message_id INTEGER,
    dedupe_key TEXT,
    priority INTEGER NOT NULL,
    disable_notification INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    attempt_count INTEGER NOT NULL,
    last_error TEXT,
    claimed_by_run_id TEXT REFERENCES service_runs(run_id),
    claimed_at TEXT,
    completed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_telegram_queue_status_available
ON telegram_outbound_queue(status, available_at);

CREATE INDEX IF NOT EXISTS idx_telegram_queue_chat
ON telegram_outbound_queue(status, chat_id, topic_id);

CREATE INDEX IF NOT EXISTS idx_telegram_queue_trace_id
ON telegram_outbound_queue(trace_id);

CREATE INDEX IF NOT EXISTS idx_telegram_queue_dedupe_key
ON telegram_outbound_queue(dedupe_key);

CREATE TABLE IF NOT EXISTS telegram_message_groups (
    message_group_id TEXT PRIMARY KEY,
    session_id TEXT REFERENCES sessions(session_id),
    trace_id TEXT REFERENCES traces(trace_id),
    chat_id INTEGER NOT NULL,
    topic_id INTEGER,
    logical_role TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    finalized_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_telegram_message_groups_session
ON telegram_message_groups(session_id);

CREATE INDEX IF NOT EXISTS idx_telegram_message_groups_trace
ON telegram_message_groups(trace_id);

CREATE TABLE IF NOT EXISTS telegram_message_chunks (
    message_group_id TEXT NOT NULL REFERENCES telegram_message_groups(message_group_id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL,
    telegram_message_id INTEGER,
    rendered_html TEXT,
    artifact_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT,
    PRIMARY KEY (message_group_id, chunk_index)
);

CREATE INDEX IF NOT EXISTS idx_telegram_chunks_message_id
ON telegram_message_chunks(telegram_message_id);

CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    relpath TEXT NOT NULL UNIQUE,
    size_bytes INTEGER NOT NULL,
    sha256 TEXT,
    created_at TEXT NOT NULL,
    expires_at TEXT,
    compressed INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_artifacts_kind_created
ON artifacts(kind, created_at);

CREATE INDEX IF NOT EXISTS idx_artifacts_expires_at
ON artifacts(expires_at);
