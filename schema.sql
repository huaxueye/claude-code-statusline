-- Session usage tracking table for claude-code-statusline
-- Create once (e.g. sqlite3 ~/.claude-cost-tracker/usage.db < schema.sql)
CREATE TABLE IF NOT EXISTS sessions (
    session_id   TEXT PRIMARY KEY,
    timestamp    TEXT NOT NULL,
    project      TEXT NOT NULL DEFAULT '',
    model        TEXT NOT NULL DEFAULT '',
    total_cost_usd      REAL NOT NULL DEFAULT 0,
    total_input_tokens  INTEGER NOT NULL DEFAULT 0,
    total_output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens   INTEGER NOT NULL DEFAULT 0,
    total_duration_ms   INTEGER NOT NULL DEFAULT 0,
    updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_date ON sessions(timestamp);
CREATE INDEX IF NOT EXISTS idx_sessions_project ON sessions(project);
