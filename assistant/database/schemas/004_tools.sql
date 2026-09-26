-- Tools registry
CREATE TABLE IF NOT EXISTS tools (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  capability TEXT NOT NULL,
  version TEXT NOT NULL,
  risk_level TEXT NOT NULL,  -- "read_only" | "reversible" | "destructive"
  declaration TEXT NOT NULL, -- JSON: inputs schema, side_effects, timeout_ms, memory_limit_mb, allow_list
  success_rate REAL NOT NULL DEFAULT 1.0,
  last_used_at INTEGER,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  status TEXT NOT NULL       -- "active" | "deprecated" | "retired"
);

-- Tool execution log
CREATE TABLE IF NOT EXISTS tool_logs (
  id TEXT PRIMARY KEY,
  tool_id TEXT NOT NULL,     -- references tools(id) loosely
  plan_step_id TEXT,         -- null if called directly
  input TEXT NOT NULL,       -- JSON input
  output TEXT,               -- JSON output
  success INTEGER NOT NULL,  -- 0 | 1
  error TEXT,
  duration_ms INTEGER,
  executed_at INTEGER NOT NULL,
  created_at INTEGER NOT NULL
);
