-- Cloud Escalation Response Cache
CREATE TABLE IF NOT EXISTS cloud_cache (
  cache_key TEXT PRIMARY KEY,
  query TEXT NOT NULL,
  intent TEXT NOT NULL,
  response TEXT NOT NULL,
  token_usage INTEGER NOT NULL,
  created_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_cloud_cache_expires ON cloud_cache(expires_at);
