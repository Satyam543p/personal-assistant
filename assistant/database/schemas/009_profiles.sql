-- Workspace Profiles Registry
CREATE TABLE IF NOT EXISTS workspace_profiles (
  name TEXT PRIMARY KEY,        -- "coding" | "study" | "research" | "project"
  display_name TEXT NOT NULL,
  description TEXT,
  default_project_id TEXT,
  biased_tools TEXT,            -- JSON array of tool names
  biased_categories TEXT,       -- JSON array of recommendation categories
  prewarm_resources TEXT,       -- JSON array of resource names
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);

-- Seed initial canonical profiles if not exist
INSERT OR IGNORE INTO workspace_profiles (name, display_name, description, default_project_id, biased_tools, biased_categories, prewarm_resources, created_at, updated_at)
VALUES
('coding', 'Coding Mode', 'Focus on codebase navigation, file operations, and project execution.', 'Jarvis', '["open_project", "read_file", "write_file", "search_files", "save_workspace"]', '["project", "task", "coding"]', '[]', strftime('%s','now'), strftime('%s','now')),
('study', 'Study Mode', 'Focus on learning goals, skill development, and study tracking.', NULL, '["growth_dashboard", "continue_working"]', '["goal", "skill", "study"]', '["growth_engine"]', strftime('%s','now'), strftime('%s','now')),
('research', 'Research Mode', 'Focus on deep inquiry, topic exploration, and literature synthesis.', NULL, '["search_files", "read_file"]', '["research", "exploration", "topic"]', '["cloud_client"]', strftime('%s','now'), strftime('%s','now')),
('project', 'Project Mode', 'Focus on multi-step workflows, planning, and task execution.', 'Jarvis', '["continue_working", "save_workspace"]', '["project", "workflow", "plan"]', '["planner"]', strftime('%s','now'), strftime('%s','now'));
