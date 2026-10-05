-- Schema 012: Reminders & One-Shot Timers Subsystem
-- Manages persistent user reminders integrated into JarvisBackgroundScheduler

CREATE TABLE IF NOT EXISTS reminders (
    id TEXT PRIMARY KEY,
    reminder_text TEXT NOT NULL,
    trigger_time_utc TEXT NOT NULL,
    trigger_time_ist TEXT,
    spoken_time TEXT,
    is_active INTEGER DEFAULT 1,
    created_at TEXT NOT NULL,
    triggered_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_reminders_active_due ON reminders(is_active, trigger_time_utc);
