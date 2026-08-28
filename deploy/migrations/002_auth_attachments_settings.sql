CREATE TABLE IF NOT EXISTS attachments (
    id SERIAL PRIMARY KEY,
    intake_id INTEGER NOT NULL REFERENCES intake_sessions(id),
    task_id INTEGER REFERENCES tasks(id),
    original_name VARCHAR(255) NOT NULL,
    stored_name VARCHAR(255) NOT NULL UNIQUE,
    content_type VARCHAR(128) NOT NULL,
    size_bytes INTEGER NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_attachments_intake_id ON attachments (intake_id);
CREATE INDEX IF NOT EXISTS ix_attachments_task_id ON attachments (task_id);
CREATE TABLE IF NOT EXISTS system_settings (
    key VARCHAR(64) PRIMARY KEY,
    value VARCHAR(255) NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT now()
);

GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE attachments, system_settings TO ops_orchestrator;
GRANT USAGE, SELECT ON SEQUENCE attachments_id_seq TO ops_orchestrator;
