-- Project registry
CREATE TABLE IF NOT EXISTS projects (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  aliases TEXT,               -- JSON array
  folder_path TEXT NOT NULL,
  preferred_editor TEXT,
  run_command TEXT,
  tags TEXT,                  -- JSON array
  description TEXT,
  related_notes TEXT,
  last_opened_at INTEGER,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);

-- Per-project workspace snapshots
CREATE TABLE IF NOT EXISTS workspace_snapshots (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,     -- references projects(id) loosely
  snapshot_type TEXT NOT NULL,  -- "session_end" | "manual" | "auto_interval"
  open_files TEXT,              -- JSON array of file paths
  editor_state TEXT,            -- JSON: scroll position, cursor, split layout
  active_branch TEXT,           -- git branch if applicable
  captured_at INTEGER NOT NULL,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);
