CREATE INDEX IF NOT EXISTS idx_telegram_queue_ready_scan
ON telegram_outbound_queue(status, available_at, priority, created_at);

CREATE INDEX IF NOT EXISTS idx_telegram_queue_message_group_status
ON telegram_outbound_queue(message_group_id, status, created_at);
