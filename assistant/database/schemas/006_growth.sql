-- Goals
CREATE TABLE IF NOT EXISTS goals (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  description TEXT,
  category TEXT,           -- "skill" | "project" | "habit" | "career" | "study"
  status TEXT NOT NULL,    -- "active" | "paused" | "completed" | "abandoned"
  target_date INTEGER,
  progress_pct REAL NOT NULL DEFAULT 0.0,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);

-- Goal progress events
CREATE TABLE IF NOT EXISTS goal_events (
  id TEXT PRIMARY KEY,
  goal_id TEXT NOT NULL,     -- references goals(id) loosely
  event_type TEXT NOT NULL,  -- "progress_update" | "milestone" | "note" | "completed"
  value REAL,
  note TEXT,
  occurred_at INTEGER NOT NULL,
  created_at INTEGER NOT NULL
);

-- Skill tracking
CREATE TABLE IF NOT EXISTS skills (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  category TEXT,            -- "programming" | "tool" | "domain" | "language" etc.
  level TEXT NOT NULL,      -- "beginner" | "intermediate" | "advanced" | "expert"
  confidence REAL NOT NULL DEFAULT 0.5,
  last_practiced_at INTEGER,
  related_goal_ids TEXT,    -- JSON array of goal_ids
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);

-- Study / learning sessions
CREATE TABLE IF NOT EXISTS study_sessions (
  id TEXT PRIMARY KEY,
  topic TEXT NOT NULL,
  skill_id TEXT,            -- references skills(id) loosely
  resource_url TEXT,
  resource_type TEXT,       -- "video" | "article" | "book" | "course" | "hands_on"
  duration_minutes INTEGER,
  notes TEXT,
  quality REAL,             -- 0.0-1.0 self-rated or inferred
  started_at INTEGER NOT NULL,
  ended_at INTEGER,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);

-- Recommendation history
CREATE TABLE IF NOT EXISTS recommendations (
  id TEXT PRIMARY KEY,
  recommendation_text TEXT NOT NULL,
  basis TEXT NOT NULL,       -- JSON data
  accepted INTEGER,          -- null=not yet, 0=rejected, 1=accepted
  outcome TEXT,              -- null | "completed" | "abandoned"
  generated_at INTEGER NOT NULL,
  responded_at INTEGER,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);
