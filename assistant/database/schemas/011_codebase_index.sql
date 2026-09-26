-- Schema 011: Codebase & Document Indexing (Local RAG)
CREATE TABLE IF NOT EXISTS document_chunks (
    id TEXT PRIMARY KEY,
    project_id TEXT,
    file_path TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    content TEXT NOT NULL,
    embedding BLOB,
    token_count INTEGER DEFAULT 0,
    created_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_doc_chunks_project ON document_chunks(project_id);
CREATE INDEX IF NOT EXISTS idx_doc_chunks_file ON document_chunks(file_path);
