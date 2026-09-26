"""
Local Codebase & Document Ingestion Engine (Local RAG) for Jarvis.
Provides intelligent chunking, on-demand embedding generation via EmbeddingManager
(0 MB idle RAM), and hybrid lexical/vector cosine search over local codebases and docs.
"""

import abc
import fnmatch
import json
import logging
import math
import os
import struct
import time
import uuid

logger = logging.getLogger("jarvis.indexer")

try:
    from assistant.tools import Tool
except ModuleNotFoundError:
    from tools import Tool


def cosine_similarity(v1: list[float], v2: list[float]) -> float:
    if not v1 or not v2 or len(v1) != len(v2):
        return 0.0
    dot = sum(a * b for a, b in zip(v1, v2))
    norm1 = math.sqrt(sum(a * a for a in v1))
    norm2 = math.sqrt(sum(b * b for b in v2))
    if norm1 == 0.0 or norm2 == 0.0:
        return 0.0
    return dot / (norm1 * norm2)


class DocumentChunker(abc.ABC):
    @abc.abstractmethod
    def chunk_text(self, text: str, chunk_size: int = 500, overlap: int = 50) -> list[str]:
        """Splits document text into overlapping segments."""
        pass


class SimpleTextChunker(DocumentChunker):
    def chunk_text(self, text: str, chunk_size: int = 600, overlap: int = 60) -> list[str]:
        if not text or not text.strip():
            return []
        
        lines = text.splitlines(keepends=True)
        chunks = []
        current_chunk = []
        current_len = 0

        for line in lines:
            line_len = len(line)
            if current_len + line_len > chunk_size and current_chunk:
                chunks.append("".join(current_chunk).strip())
                # Keep last few lines for overlap
                overlap_chunk = []
                overlap_len = 0
                for prev_line in reversed(current_chunk):
                    if overlap_len + len(prev_line) <= overlap:
                        overlap_chunk.insert(0, prev_line)
                        overlap_len += len(prev_line)
                    else:
                        break
                current_chunk = overlap_chunk
                current_len = overlap_len

            current_chunk.append(line)
            current_len += line_len

        if current_chunk and "".join(current_chunk).strip():
            chunks.append("".join(current_chunk).strip())

        return chunks


class CodebaseIndexer(abc.ABC):
    @abc.abstractmethod
    async def index_directory(self, folder_path: str, project_id: str | None = None, file_extensions: list[str] | None = None) -> dict:
        """Indexes files in a folder into SQLite document_chunks."""
        pass

    @abc.abstractmethod
    async def search_knowledge(self, query: str, project_id: str | None = None, top_k: int = 5) -> list[dict]:
        """Performs semantic and lexical search over indexed codebase chunks."""
        pass


class LocalCodebaseIndexer(CodebaseIndexer):
    def __init__(self, db_manager, embedding_manager=None, chunker: DocumentChunker = None):
        self.db = db_manager
        self.embedding_manager = embedding_manager
        self.chunker = chunker or SimpleTextChunker()

    def _pack_vector(self, vector: list[float]) -> bytes:
        return struct.pack(f"{len(vector)}f", *vector)

    def _unpack_vector(self, blob: bytes) -> list[float]:
        num_floats = len(blob) // 4
        return list(struct.unpack(f"{num_floats}f", blob))

    async def index_directory(self, folder_path: str, project_id: str | None = None, file_extensions: list[str] | None = None) -> dict:
        if not os.path.exists(folder_path):
            raise FileNotFoundError(f"Folder path not found: {folder_path}")

        extensions = file_extensions or [".py", ".md", ".txt", ".json", ".sql", ".js", ".ts", ".html"]
        norm_exts = {e.lower() if e.startswith(".") else f".{e.lower()}" for e in extensions}

        files_to_index = []
        for root, dirs, files in os.walk(folder_path):
            # Ignore hidden or build dirs
            dirs[:] = [d for d in dirs if not d.startswith(".") and d not in ("__pycache__", "node_modules", "dist", "build", "venv", ".git")]
            for file in files:
                _, ext = os.path.splitext(file)
                if ext.lower() in norm_exts:
                    files_to_index.append(os.path.join(root, file))

        total_chunks = 0
        indexed_files = 0
        now = int(time.time())

        for fpath in files_to_index:
            try:
                with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read(150 * 1024)  # 150 KB cap per file
                if not content.strip():
                    continue

                chunks = self.chunker.chunk_text(content)
                if not chunks:
                    continue

                # Delete existing chunks for this file
                with self.db.transaction() as conn:
                    conn.execute("DELETE FROM document_chunks WHERE file_path = ?;", (fpath,))
                    for idx, chunk in enumerate(chunks):
                        chunk_id = str(uuid.uuid4())
                        emb_blob = None
                        if self.embedding_manager:
                            try:
                                emb = self.embedding_manager.embed_text(chunk)
                                if emb:
                                    emb_blob = self._pack_vector(emb)
                            except Exception as emb_err:
                                logger.debug(f"Chunk embedding generation skipped: {emb_err}")

                        conn.execute(
                            "INSERT INTO document_chunks (id, project_id, file_path, chunk_index, content, embedding, token_count, created_at) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?);",
                            (chunk_id, project_id, fpath, idx, chunk, emb_blob, len(chunk.split()), now)
                        )
                        total_chunks += 1
                indexed_files += 1
            except Exception as e:
                logger.error(f"Error indexing file {fpath}: {e}")

        return {
            "status": "success",
            "folder_path": folder_path,
            "project_id": project_id,
            "files_indexed": indexed_files,
            "chunks_created": total_chunks
        }

    async def search_knowledge(self, query: str, project_id: str | None = None, top_k: int = 5) -> list[dict]:
        if not query or not query.strip():
            return []

        query_emb = None
        if self.embedding_manager:
            try:
                query_emb = self.embedding_manager.embed_text(query)
            except Exception as e:
                logger.debug(f"Query embedding generation skipped: {e}")

        results = []
        with self.db.transaction() as conn:
            cursor = conn.cursor()
            if project_id:
                cursor.execute(
                    "SELECT id, project_id, file_path, chunk_index, content, embedding FROM document_chunks WHERE project_id = ?;",
                    (project_id,)
                )
            else:
                cursor.execute(
                    "SELECT id, project_id, file_path, chunk_index, content, embedding FROM document_chunks LIMIT 500;"
                )
            rows = cursor.fetchall()

        query_terms = [t.lower() for t in query.split() if len(t) > 2]

        for row in rows:
            chunk_text = row["content"]
            chunk_lower = chunk_text.lower()
            
            # 1. Lexical score
            lexical_score = 0.0
            if query.lower() in chunk_lower:
                lexical_score += 0.5
            term_matches = sum(1 for t in query_terms if t in chunk_lower)
            if query_terms:
                lexical_score += 0.5 * (term_matches / len(query_terms))

            # 2. Vector score
            vector_score = 0.0
            if query_emb and row["embedding"]:
                stored_emb = self._unpack_vector(row["embedding"])
                vector_score = cosine_similarity(query_emb, stored_emb)

            combined_score = (vector_score * 0.6) + (lexical_score * 0.4) if query_emb else lexical_score
            if combined_score > 0.15:
                results.append({
                    "id": row["id"],
                    "project_id": row["project_id"],
                    "file_path": row["file_path"],
                    "chunk_index": row["chunk_index"],
                    "content": chunk_text,
                    "score": round(combined_score, 4)
                })

        results.sort(key=lambda x: x["score"], reverse=True)
        return results[:top_k]


class IndexCodebaseTool(Tool):
    def __init__(self, indexer: CodebaseIndexer = None):
        declaration = {
            "inputs": {
                "folder_path": {"type": "string"},
                "project_id": {"type": "string"}
            },
            "side_effects": "Chunks, embeds, and indexes code & documentation into SQLite",
            "timeout_ms": 30000,
            "memory_limit_mb": 150
        }
        super().__init__("index_codebase", "reversible", declaration)
        self.indexer = indexer

    async def execute(self, executor, **kwargs) -> dict:
        indexer = self.indexer or getattr(executor, "codebase_indexer", None)
        if not indexer:
            raise RuntimeError("Codebase indexer subsystem not configured on executor.")

        folder_path = kwargs.get("folder_path") or kwargs.get("path") or "."
        if folder_path == ".":
            folder_path = os.getcwd()

        project_id = kwargs.get("project_id") or os.path.basename(folder_path.rstrip(r"\/"))
        res = await indexer.index_directory(folder_path, project_id=project_id)
        return {
            "status": "success",
            "index_result": res,
            "response": f"Successfully indexed '{folder_path}': {res['files_indexed']} files, {res['chunks_created']} chunks stored."
        }


class SearchCodebaseTool(Tool):
    def __init__(self, indexer: CodebaseIndexer = None):
        declaration = {
            "inputs": {
                "query": {"type": "string"},
                "project_id": {"type": "string"},
                "top_k": {"type": "integer", "default": 5}
            },
            "side_effects": "Performs semantic and lexical knowledge search over indexed codebase",
            "timeout_ms": 5000,
            "memory_limit_mb": 50
        }
        super().__init__("search_codebase", "read_only", declaration)
        self.indexer = indexer

    async def execute(self, executor, **kwargs) -> dict:
        indexer = self.indexer or getattr(executor, "codebase_indexer", None)
        if not indexer:
            raise RuntimeError("Codebase indexer subsystem not configured on executor.")

        query = kwargs.get("query") or kwargs.get("search_query") or ""
        project_id = kwargs.get("project_id")
        top_k = kwargs.get("top_k", 5)

        if not query:
            raise ValueError("Query parameter 'query' is required for search_codebase.")

        matches = await indexer.search_knowledge(query, project_id=project_id, top_k=top_k)
        summary = f"Found {len(matches)} relevant codebase chunks for '{query}'."

        return {
            "status": "success",
            "matches": matches,
            "response": summary
        }
