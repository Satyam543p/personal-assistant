-- Schema version tracking
CREATE TABLE IF NOT EXISTS schema_meta (
  version INTEGER PRIMARY KEY,
  applied_at INTEGER NOT NULL
);

-- User profile (single row)
CREATE TABLE IF NOT EXISTS profile (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  name TEXT,
  timezone TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);

-- Long-term preferences (key-value)
CREATE TABLE IF NOT EXISTS preferences (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);

-- Active context (single-session, fast-access)
CREATE TABLE IF NOT EXISTS active_context (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at INTEGER NOT NULL
);

-- Current focus pointer (single row)
CREATE TABLE IF NOT EXISTS current_focus (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  project_id TEXT,
  workspace_mode TEXT,
  updated_at INTEGER NOT NULL
);

-- System error log
CREATE TABLE IF NOT EXISTS system_errors (
  id TEXT PRIMARY KEY,
  component TEXT NOT NULL,  -- "router" | "planner" | "memory" | "sandbox" | "cloud" | "interpreter"
  severity TEXT NOT NULL,   -- "debug" | "info" | "warning" | "error" | "critical"
  message TEXT NOT NULL,
  context TEXT,             -- JSON data
  occurred_at INTEGER NOT NULL,
  created_at INTEGER NOT NULL
);
