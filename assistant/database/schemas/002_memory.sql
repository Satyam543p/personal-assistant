-- Durable memories
CREATE TABLE IF NOT EXISTS memories (
  id TEXT PRIMARY KEY,
  category TEXT NOT NULL,  -- "identity" | "preference" | "goal" | "habit" | "decision" | "project_context"
  scope TEXT NOT NULL,     -- "global" | project_id for project-specific memories
  content TEXT NOT NULL,
  embedding BLOB,          -- stored only when semantic layer is active
  importance_score REAL NOT NULL DEFAULT 0.5,
  source TEXT NOT NULL,    -- "explicit_statement" | "inferred" | "tool_result"
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  last_accessed_at INTEGER,
  access_count INTEGER NOT NULL DEFAULT 0,
  superseded_by TEXT,      -- memory id that replaces this one (archive, not delete)
  specificity_weight REAL NOT NULL DEFAULT 1.0
);

-- Conversation summaries
CREATE TABLE IF NOT EXISTS conversation_summaries (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  summary TEXT NOT NULL,
  key_entities TEXT,         -- JSON array of extracted entities
  started_at INTEGER NOT NULL,
  ended_at INTEGER NOT NULL,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);
