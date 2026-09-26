-- Plans
CREATE TABLE IF NOT EXISTS plans (
  id TEXT PRIMARY KEY,
  goal TEXT NOT NULL,
  status TEXT NOT NULL,  -- pending | running | done | failed
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  completed_at INTEGER
);

-- Plan steps
CREATE TABLE IF NOT EXISTS plan_steps (
  id TEXT PRIMARY KEY,
  plan_id TEXT NOT NULL,     -- references plans(id) loosely
  index_order INTEGER NOT NULL,
  intent TEXT NOT NULL,
  inputs TEXT NOT NULL,  -- JSON
  depends_on TEXT,       -- JSON array of step ids
  risk_level TEXT NOT NULL,
  status TEXT NOT NULL,
  result TEXT,           -- JSON
  error TEXT,
  rollback_action TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);
