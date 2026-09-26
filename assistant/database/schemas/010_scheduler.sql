-- Schema 010: Autonomous Background Scheduler & Proactive Cron Engine
-- Manages background jobs, persistent schedules, execution audits, and proactive feed

CREATE TABLE IF NOT EXISTS scheduled_jobs (
    id TEXT PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    description TEXT,
    schedule_type TEXT NOT NULL,     -- 'interval', 'cron', 'daily_at'
    schedule_value TEXT NOT NULL,    -- '3600' (seconds), '03:00' (HH:MM), etc.
    priority INTEGER DEFAULT 5,      -- 1 (highest) to 10 (lowest)
    is_active INTEGER DEFAULT 1,
    last_run_at TEXT,
    next_run_at TEXT,
    run_count INTEGER DEFAULT 0,
    failure_count INTEGER DEFAULT 0,
    last_status TEXT,                -- 'success', 'failed', 'running', 'skipped'
    last_error TEXT,
    metadata TEXT,                   -- JSON object with job-specific parameters
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS job_execution_logs (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    job_name TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    duration_ms INTEGER DEFAULT 0,
    status TEXT NOT NULL,            -- 'success', 'failed', 'skipped'
    summary TEXT,
    error_message TEXT,
    metadata TEXT,                   -- JSON object
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS proactive_feed (
    id TEXT PRIMARY KEY,
    feed_type TEXT NOT NULL,         -- 'morning_briefing', 'evening_digest', 'system_alert', 'recommendation'
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    priority INTEGER DEFAULT 5,
    is_read INTEGER DEFAULT 0,
    metadata TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_scheduled_jobs_active ON scheduled_jobs(is_active);
CREATE INDEX IF NOT EXISTS idx_scheduled_jobs_next_run ON scheduled_jobs(next_run_at);
CREATE INDEX IF NOT EXISTS idx_job_execution_logs_job ON job_execution_logs(job_id);
CREATE INDEX IF NOT EXISTS idx_job_execution_logs_started ON job_execution_logs(started_at);
CREATE INDEX IF NOT EXISTS idx_proactive_feed_unread ON proactive_feed(is_read, created_at);
