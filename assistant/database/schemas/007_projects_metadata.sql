-- Migration 007: Expand projects registry metadata fields
ALTER TABLE projects ADD COLUMN project_type TEXT;
ALTER TABLE projects ADD COLUMN git_repo_info TEXT; -- JSON string containing Git repository status/remotes
ALTER TABLE projects ADD COLUMN last_modified_at INTEGER;
ALTER TABLE projects ADD COLUMN is_pinned INTEGER DEFAULT 0;
ALTER TABLE projects ADD COLUMN is_archived INTEGER DEFAULT 0;
ALTER TABLE projects ADD COLUMN active_workspace_id TEXT;
ALTER TABLE projects ADD COLUMN metadata TEXT; -- JSON string representing extensible dictionary for future modules
