-- Schema 013: Learned and Seed Knowledge Base
CREATE TABLE IF NOT EXISTS knowledge_items (
    id TEXT PRIMARY KEY,
    category TEXT NOT NULL,
    key TEXT,
    content TEXT NOT NULL,
    source TEXT NOT NULL,
    importance_weight REAL DEFAULT 0.5,
    file_hash TEXT,
    is_active INTEGER DEFAULT 1,
    superseded_by TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_knowledge_active ON knowledge_items(is_active, category);
CREATE INDEX IF NOT EXISTS idx_knowledge_key ON knowledge_items(key);
