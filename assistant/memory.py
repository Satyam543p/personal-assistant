import abc
import asyncio
import gc
import json
import logging
import math
import os
import re
import struct
import time
import zlib
from collections import Counter
from dataclasses import dataclass

try:
    from assistant.config import (
        EMBEDDING_PROVIDER,
        EMBEDDING_MODEL_NAME,
        EMBEDDING_DIM,
        EMBEDDING_IDLE_TIMEOUT,
        MEMORY_TOP_K,
        MEMORY_HALF_LIFE_DAYS,
        MEMORY_SIMILARITY_CONFLICT_THRESHOLD,
        MEMORY_PRUNE_THRESHOLD,
        MEMORY_PRUNE_MIN_AGE_DAYS,
    )
    from assistant.router import MemoryManager
    from assistant.tools import Tool
except ModuleNotFoundError:
    from config import (
        EMBEDDING_PROVIDER,
        EMBEDDING_MODEL_NAME,
        EMBEDDING_DIM,
        EMBEDDING_IDLE_TIMEOUT,
        MEMORY_TOP_K,
        MEMORY_HALF_LIFE_DAYS,
        MEMORY_SIMILARITY_CONFLICT_THRESHOLD,
        MEMORY_PRUNE_THRESHOLD,
        MEMORY_PRUNE_MIN_AGE_DAYS,
    )
    from router import MemoryManager
    from tools import Tool

logger = logging.getLogger("jarvis.memory")


# =====================================================================
# Data Structures
# =====================================================================

@dataclass
class MemoryRecord:
    id: str
    category: str         # "identity" | "preference" | "goal" | "habit" | "decision" | "project_context"
    scope: str            # "global" | project_id
    content: str
    embedding: bytes | None = None
    importance_score: float = 0.5
    source: str = "explicit_statement"   # "explicit_statement" | "inferred" | "tool_result"
    created_at: int = 0
    updated_at: int = 0
    last_accessed_at: int | None = None
    access_count: int = 0
    superseded_by: str | None = None
    specificity_weight: float = 1.0
    score: float = 0.0    # Computed hybrid score for ranking
    superseded_records: list[str] | None = None  # IDs of records superseded by this record


# =====================================================================
# Specificity & Importance Scorer Heuristics (Section 6 & 19)
# =====================================================================

def compute_specificity(content: str) -> float:
    """
    Computes a deterministic specificity weight based on sentence features:
    - Detailed entities, numbers, versions, file paths (+0.2 each)
    - Qualifying clauses (when, because, using, with, instead of) (+0.15 each)
    - Proper nouns or capitalized multi-word terms (+0.15)
    - Length bonus (+0.1 * log(len / 20))
    Clamped between 0.5 and 3.0.
    """
    if not content:
        return 1.0

    score = 1.0
    clean = content.strip()

    # Numbers, version strings, ports, or file paths
    technical_entities = re.findall(r"\b(?:\d+\.\d+(?:\.\d+)?|v\d+|\d{2,5}|[a-zA-Z0-9_\-\]+\.[a-zA-Z0-9]{2,4})\b", clean)
    score += min(0.6, len(technical_entities) * 0.2)

    # Qualifying clauses
    qualifiers = re.findall(r"\b(when|whenever|because|using|with|instead of|rather than|specifically|always|never)\b", clean, re.IGNORECASE)
    score += min(0.45, len(qualifiers) * 0.15)

    # Capitalized terms (e.g. Tokyo Night, VS Code, Python, React)
    capitalized = re.findall(r"\b[A-Z][a-zA-Z0-9_]{2,}\b", clean)
    if len(capitalized) >= 2:
        score += min(0.3, len(capitalized) * 0.1)

    # Length logarithmic factor
    if len(clean) > 20:
        score += min(0.4, 0.1 * math.log(len(clean) / 20.0))

    return round(max(0.5, min(3.0, score)), 4)


def compute_initial_importance(category: str, source: str, specificity: float) -> float:
    """
    Computes initial importance score:
    Source Priority: explicit_statement (0.85) > tool_result (0.70) > inferred (0.55).
    Category Modifier: identity (+0.15), preference (+0.10), decision (+0.08), goal (+0.05), habit (+0.05).
    Modulated by specificity weight. Clamped to [0.1, 1.0].
    """
    source_map = {
        "explicit_statement": 0.85,
        "tool_result": 0.70,
        "inferred": 0.55
    }
    base = source_map.get(source.lower().strip(), 0.65)

    category_modifiers = {
        "identity": 0.15,
        "preference": 0.10,
        "decision": 0.08,
        "goal": 0.05,
        "habit": 0.05,
        "project_context": 0.0
    }
    mod = category_modifiers.get(category.lower().strip(), 0.0)

    # Specificity modulation
    spec_factor = 0.85 + 0.15 * min(2.0, max(0.5, specificity))
    final_score = (base + mod) * spec_factor
    return round(max(0.1, min(1.0, final_score)), 4)


def compute_effective_importance(record: MemoryRecord, current_time: int | None = None) -> float:
    """
    Computes current dynamic effective importance score:
    1. Base importance
    2. Access frequency boost: min(0.3, 0.05 * ln(1 + access_count))
    3. Recency decay:
       - Identity category has 365-day half-life and 0.40 floor (core facts do not rapidly decay).
       - Other categories have configurable 90-day half-life and 0.10 floor.
    Clamped to [floor, 1.0].
    """
    now = current_time or int(time.time())
    base = record.importance_score

    # Access frequency logarithm boost
    access_boost = min(0.30, 0.05 * math.log(1 + max(0, record.access_count)))

    # Decay parameters
    if record.category.lower().strip() == "identity":
        half_life_days = 365.0
        floor = 0.40
    else:
        half_life_days = float(MEMORY_HALF_LIFE_DAYS)
        floor = 0.10

    t_diff = max(0, now - (record.updated_at or record.created_at or now))
    decay = 0.5 ** (t_diff / (half_life_days * 86400.0))

    effective = max(floor, (base + access_boost) * decay)
    return round(max(floor, min(1.0, effective)), 4)


# =====================================================================
# Vector Utilities (Packing, Unpacking, Cosine Similarity)
# =====================================================================

def pack_vector(vec: list[float]) -> bytes:
    """Serializes a float vector into binary BLOB."""
    return struct.pack(f"{len(vec)}f", *vec)


def unpack_vector(blob: bytes) -> list[float]:
    """Deserializes a binary BLOB into a float vector."""
    if not blob:
        return []
    num_floats = len(blob) // 4
    return list(struct.unpack(f"{num_floats}f", blob))


def cosine_similarity(v1: list[float], v2: list[float]) -> float:
    """Computes cosine similarity between two float vectors."""
    if not v1 or not v2 or len(v1) != len(v2):
        return 0.0

    dot = 0.0
    norm1 = 0.0
    norm2 = 0.0
    for a, b in zip(v1, v2):
        dot += a * b
        norm1 += a * a
        norm2 += b * b

    if norm1 <= 0.0 or norm2 <= 0.0:
        return 0.0

    sim = dot / (math.sqrt(norm1) * math.sqrt(norm2))
    return max(0.0, min(1.0, float(sim)))


# =====================================================================
# Embedding Provider Abstractions & Implementations
# =====================================================================

class EmbeddingProvider(abc.ABC):
    """Abstract base class for swappable embedding providers."""

    @property
    @abc.abstractmethod
    def dimension(self) -> int:
        pass

    @property
    @abc.abstractmethod
    def provider_name(self) -> str:
        pass

    @abc.abstractmethod
    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        pass

    async def embed_text(self, text: str) -> list[float]:
        results = await self.embed_texts([text])
        return results[0] if results else [0.0] * self.dimension


class HashTFIDFEmbeddingProvider(EmbeddingProvider):
    """
    Deterministic, pure-Python fallback embedding provider.
    Computes character n-grams and token hashes normalized into a 384-dim unit vector.
    Zero external dependencies, zero network requests, instant startup, reproducible.
    """

    def __init__(self, dim: int = EMBEDDING_DIM):
        self._dim = dim

    @property
    def dimension(self) -> int:
        return self._dim

    @property
    def provider_name(self) -> str:
        return "hash_tfidf_fallback"

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_single(t) for t in texts]

    def _embed_single(self, text: str) -> list[float]:
        vec = [0.0] * self._dim
        if not text:
            return vec

        cleaned = text.lower().strip()
        tokens = re.findall(r"\b\w+\b", cleaned)
        char_ngrams = []
        for i in range(len(cleaned) - 2):
            char_ngrams.append(cleaned[i:i + 3])

        features = tokens + char_ngrams
        if not features:
            return vec

        counts = Counter(features)
        total = len(features)

        for feat, count in counts.items():
            tf = 1.0 + math.log(count)
            h = zlib.crc32(feat.encode("utf-8"))
            bucket = h % self._dim
            sign = 1.0 if (h & 0x80000000) == 0 else -1.0
            vec[bucket] += sign * tf / total

        norm = math.sqrt(sum(x * x for x in vec))
        if norm > 0:
            vec = [round(x / norm, 6) for x in vec]
        return vec


class SentenceTransformersEmbeddingProvider(EmbeddingProvider):
    """SentenceTransformers provider running locally."""

    def __init__(self, model_name: str = EMBEDDING_MODEL_NAME):
        self._model_name = model_name
        self._model = None
        self._dim = EMBEDDING_DIM

    @property
    def dimension(self) -> int:
        return self._dim

    @property
    def provider_name(self) -> str:
        return "sentence_transformers"

    def load(self):
        if self._model is None:
            from sentence_transformers import SentenceTransformer
            try:
                self._model = SentenceTransformer(self._model_name, local_files_only=True)
            except Exception:
                self._model = SentenceTransformer(self._model_name)

            if hasattr(self._model, "get_embedding_dimension"):
                self._dim = self._model.get_embedding_dimension()
            else:
                self._dim = self._model.get_sentence_embedding_dimension()

    def unload(self):
        self._model = None

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        if self._model is None:
            await asyncio.to_thread(self.load)

        def _encode():
            embeddings = self._model.encode(texts, normalize_embeddings=True)
            return [e.tolist() for e in embeddings]

        return await asyncio.to_thread(_encode)


class OllamaEmbeddingProvider(EmbeddingProvider):
    """Embedding provider connecting to Ollama /api/embeddings."""

    def __init__(self, url: str = "http://localhost:11434", model: str = "all-minilm"):
        self.url = url.rstrip("/")
        self.model = model
        self._dim = EMBEDDING_DIM

    @property
    def dimension(self) -> int:
        return self._dim

    @property
    def provider_name(self) -> str:
        return "ollama_embeddings"

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        import urllib.request

        results = []
        for text in texts:
            req_data = json.dumps({"model": self.model, "prompt": text}).encode("utf-8")
            req = urllib.request.Request(
                f"{self.url}/api/embeddings",
                data=req_data,
                headers={"Content-Type": "application/json"},
                method="POST"
            )

            def _call():
                try:
                    with urllib.request.urlopen(req, timeout=2.0) as resp:
                        data = json.loads(resp.read().decode("utf-8"))
                        return data.get("embedding", [])
                except Exception as e:
                    logger.debug(f"Ollama embedding request failed: {e}")
                    return []

            emb = await asyncio.to_thread(_call)
            if emb:
                self._dim = len(emb)
                results.append(emb)
            else:
                results.append([0.0] * self._dim)
        return results


class AutoEmbeddingProvider(EmbeddingProvider):
    """
    Dynamically resolves the embedding provider:
    1. SentenceTransformers (if model loadable)
    2. Ollama /api/embeddings (if endpoint reachable and model works)
    3. HashTFIDF fallback (guaranteed offline stability)
    """

    def __init__(self):
        self._resolved_provider: EmbeddingProvider | None = None
        self._dim = EMBEDDING_DIM

    @property
    def dimension(self) -> int:
        if self._resolved_provider:
            return self._resolved_provider.dimension
        return self._dim

    @property
    def provider_name(self) -> str:
        if self._resolved_provider:
            return f"auto({self._resolved_provider.provider_name})"
        return "auto(unresolved)"

    async def resolve(self) -> EmbeddingProvider:
        if self._resolved_provider:
            return self._resolved_provider

        try:
            st = SentenceTransformersEmbeddingProvider()
            await asyncio.to_thread(st.load)
            self._resolved_provider = st
            logger.info("AutoEmbeddingProvider resolved to SentenceTransformers.")
            return self._resolved_provider
        except Exception as e:
            logger.debug(f"SentenceTransformers unavailable: {e}")

        try:
            ollama = OllamaEmbeddingProvider()
            res = await ollama.embed_texts(["test"])
            if res and any(x != 0.0 for x in res[0]):
                self._resolved_provider = ollama
                logger.info("AutoEmbeddingProvider resolved to Ollama embeddings.")
                return self._resolved_provider
        except Exception as e:
            logger.debug(f"Ollama embedding unavailable: {e}")

        self._resolved_provider = HashTFIDFEmbeddingProvider()
        logger.info("AutoEmbeddingProvider resolved to deterministic HashTFIDF fallback.")
        return self._resolved_provider

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        provider = await self.resolve()
        return await provider.embed_texts(texts)


def get_embedding_provider(provider_type: str = EMBEDDING_PROVIDER) -> EmbeddingProvider:
    """Factory creating configured embedding provider."""
    p_type = provider_type.lower().strip()
    if p_type == "sentence_transformers":
        return SentenceTransformersEmbeddingProvider()
    elif p_type == "ollama":
        return OllamaEmbeddingProvider()
    elif p_type == "fallback":
        return HashTFIDFEmbeddingProvider()
    return AutoEmbeddingProvider()


# =====================================================================
# Lazy Embedding Manager (Idle Unload Watchdog)
# =====================================================================

class LazyEmbeddingManager:
    """
    Loads embedding provider on-demand when memory operations occur.
    Registers an idle timer with IdleTimerManager to unload the model and
    free RAM after inactivity (default 60s).
    """

    def __init__(self, timer_manager=None, timeout: float = EMBEDDING_IDLE_TIMEOUT, on_state_change=None):
        self.timer_manager = timer_manager
        self.timeout = timeout
        self.on_state_change = on_state_change
        self._provider: EmbeddingProvider | None = None
        self._lock = asyncio.Lock()

    @property
    def is_loaded(self) -> bool:
        return self._provider is not None

    async def get_provider(self) -> EmbeddingProvider:
        async with self._lock:
            if self._provider is None:
                logger.info("Lazy-loading embedding model on demand...")
                self._provider = get_embedding_provider()
                if hasattr(self._provider, "resolve"):
                    await self._provider.resolve()
                if self.on_state_change:
                    try:
                        self.on_state_change("embeddings", True)
                    except Exception as e:
                        logger.error(f"Error notifying on_state_change: {e}")

            if self.timer_manager:
                await self.timer_manager.register_or_update(
                    "embeddings",
                    self.unload,
                    timeout=self.timeout
                )

            return self._provider

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        provider = await self.get_provider()
        return await provider.embed_texts(texts)

    async def embed_text(self, text: str) -> list[float]:
        provider = await self.get_provider()
        return await provider.embed_text(text)

    async def unload(self):
        async with self._lock:
            if self._provider is not None:
                logger.info("Unloading lazy embedding model to free memory...")
                if hasattr(self._provider, "unload"):
                    self._provider.unload()
                self._provider = None
                gc.collect()
                if self.on_state_change:
                    try:
                        self.on_state_change("embeddings", False)
                    except Exception as e:
                        logger.error(f"Error notifying on_state_change: {e}")
                logger.info("Embedding model unloaded successfully.")


# =====================================================================
# Subsystem Abstraction (Abstractions & Interfaces First)
# =====================================================================

class MemoryRepository(abc.ABC):
    """Abstract interface defining the durable memory subsystem contract."""

    @abc.abstractmethod
    async def store_memory(
        self,
        content: str,
        category: str = "preference",
        scope: str = "global",
        importance_score: float | None = None,
        source: str = "explicit_statement",
        specificity_weight: float | None = None,
        memory_id: str | None = None
    ) -> MemoryRecord:
        """Stores a new durable memory, resolving conflicts and archiving superseded memories."""
        pass

    @abc.abstractmethod
    async def retrieve_hybrid(
        self,
        query: str,
        category: str | None = None,
        scope: str = "global",
        include_global: bool = True,
        top_k: int = MEMORY_TOP_K,
        include_superseded: bool = False
    ) -> list[MemoryRecord]:
        """Hybrid vector + structured retrieval ranked with effective importance decay."""
        pass

    @abc.abstractmethod
    def get_memory(self, memory_id: str) -> MemoryRecord | None:
        """Fetches single memory by ID."""
        pass

    @abc.abstractmethod
    def get_memory_history(self, memory_id: str) -> list[MemoryRecord]:
        """Returns chronological lineage / revision history for a memory."""
        pass

    @abc.abstractmethod
    def prune_memories(
        self,
        threshold: float = MEMORY_PRUNE_THRESHOLD,
        min_age_days: int = MEMORY_PRUNE_MIN_AGE_DAYS,
        dry_run: bool = True
    ) -> dict:
        """Prunes decayed low-importance memories and old archived memories."""
        pass

    @abc.abstractmethod
    def get_project_memories(self, project_id: str, limit: int = 5) -> list[MemoryRecord]:
        """Retrieves active project-scoped memories ranked by effective importance."""
        pass

    @abc.abstractmethod
    def load_project_focus_context(self, project_id: str, context_store = None) -> list[MemoryRecord]:
        """Loads top-5 project-scoped memories into working context and updates current_focus."""
        pass

    @abc.abstractmethod
    def propose_project_memory(self, project_id: str, content: str, category: str = "project_context", context_store = None) -> dict:
        """Proposes a project-specific memory for opt-in user confirmation."""
        pass

    @abc.abstractmethod
    async def confirm_project_memory(self, confirm: bool = True, context_store = None) -> MemoryRecord | None:
        """Confirms and stores or rejects a proposed project memory."""
        pass


# =====================================================================
# Concrete Implementation: JarvisMemoryManager
# =====================================================================

class JarvisMemoryManager(MemoryRepository, MemoryManager):
    """
    Subsystem managing durable memories in SQLite with hybrid retrieval,
    conflict detection (superseding and archiving), recency decay, and importance scoring.
    """

    def __init__(self, db_manager, timer_manager=None, on_state_change=None):
        self.db = db_manager
        self.lazy_embedder = LazyEmbeddingManager(
            timer_manager=timer_manager,
            timeout=EMBEDDING_IDLE_TIMEOUT,
            on_state_change=on_state_change
        )

    def _row_to_record(self, row) -> MemoryRecord:
        return MemoryRecord(
            id=row["id"],
            category=row["category"],
            scope=row["scope"],
            content=row["content"],
            embedding=row["embedding"],
            importance_score=float(row["importance_score"]),
            source=row["source"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            last_accessed_at=row["last_accessed_at"],
            access_count=row["access_count"],
            superseded_by=row["superseded_by"],
            specificity_weight=float(row["specificity_weight"])
        )

    # -----------------------------------------------------------------
    # CRUD & Storage with Conflict Resolution
    # -----------------------------------------------------------------

    async def store_memory(
        self,
        content: str,
        category: str = "preference",
        scope: str = "global",
        importance_score: float | None = None,
        source: str = "explicit_statement",
        specificity_weight: float | None = None,
        memory_id: str | None = None
    ) -> MemoryRecord:
        """
        Embeds content, detects semantic conflicts with existing active memories,
        archives superseded memories by setting superseded_by, and saves the new memory.
        """
        now = int(time.time())
        m_id = memory_id or f"mem_{now}_{int(time.time() * 1000) % 1000}"

        # 1. Specificity & Importance Calculation
        spec = specificity_weight if specificity_weight is not None else compute_specificity(content)
        imp = importance_score if importance_score is not None else compute_initial_importance(category, source, spec)

        # 2. Compute embedding vector
        vec = await self.lazy_embedder.embed_text(content)
        packed_vec = pack_vector(vec)

        # 3. Conflict Handling & Superseding (Section 6)
        # Newer, specific statements supersede older similar memories in the same scope and category
        superseded_ids = []
        try:
            with self.db.transaction() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT id, content, embedding, specificity_weight, source, created_at FROM memories "
                    "WHERE scope = ? AND category = ? AND superseded_by IS NULL;",
                    (scope, category)
                )
                existing_rows = cursor.fetchall()
                for row in existing_rows:
                    e_blob = row["embedding"]
                    e_vec = unpack_vector(e_blob) if e_blob else []
                    sim = cosine_similarity(vec, e_vec)
                    old_spec = float(row["specificity_weight"])
                    old_source = row["source"]

                    # Conflict detection threshold:
                    # An explicit user statement supersedes an inferred statement, OR
                    # High semantic overlap (>= threshold) with equal/higher specificity
                    is_conflict = False
                    if sim >= MEMORY_SIMILARITY_CONFLICT_THRESHOLD:
                        if source == "explicit_statement" and old_source == "inferred":
                            is_conflict = True
                        elif spec >= (old_spec * 0.9):
                            is_conflict = True

                    if is_conflict:
                        superseded_ids.append(row["id"])

                # Mark superseded memories (Archive, do not delete)
                for old_id in superseded_ids:
                    logger.info(f"Memory '{old_id}' superseded by new memory '{m_id}'")
                    conn.execute(
                        "UPDATE memories SET superseded_by = ?, updated_at = ? WHERE id = ?;",
                        (m_id, now, old_id)
                    )

                # Insert new memory
                conn.execute(
                    "INSERT INTO memories (id, category, scope, content, embedding, importance_score, "
                    "source, created_at, updated_at, last_accessed_at, access_count, superseded_by, specificity_weight) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);",
                    (
                        m_id, category, scope, content, packed_vec, imp,
                        source, now, now, now, 0, None, spec
                    )
                )
        except Exception as e:
            logger.error(f"Error persisting memory '{m_id}': {e}")
            raise

        logger.info(f"Stored memory '{m_id}' ({category}/{scope}, imp={imp}, spec={spec}) with {len(superseded_ids)} superseded.")
        return MemoryRecord(
            id=m_id,
            category=category,
            scope=scope,
            content=content,
            embedding=packed_vec,
            importance_score=imp,
            source=source,
            created_at=now,
            updated_at=now,
            last_accessed_at=now,
            access_count=0,
            superseded_by=None,
            specificity_weight=spec,
            superseded_records=superseded_ids
        )

    def get_memory(self, memory_id: str) -> MemoryRecord | None:
        try:
            with self.db.transaction() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT * FROM memories WHERE id = ?;", (memory_id,))
                row = cursor.fetchone()
                return self._row_to_record(row) if row else None
        except Exception as e:
            logger.error(f"Error fetching memory '{memory_id}': {e}")
            return None

    def get_memory_history(self, memory_id: str) -> list[MemoryRecord]:
        """
        Traces backward and forward through superseded_by links to return
        the full lineage/revision history of this fact.
        """
        history = []
        visited = set()

        curr_id = memory_id
        # If curr_id is not directly an ID, search by content keyword match
        if curr_id and not self.get_memory(curr_id):
            try:
                with self.db.transaction() as conn:
                    cursor = conn.cursor()
                    words = [w for w in re.findall(r"\w+", curr_id.lower()) if len(w) > 2]
                    if words:
                        like_clauses = " OR ".join(["LOWER(content) LIKE ?" for _ in words])
                        params = [f"%{w}%" for w in words]
                        cursor.execute(
                            f"SELECT id FROM memories WHERE {like_clauses} ORDER BY updated_at DESC LIMIT 1;",
                            tuple(params)
                        )
                        row = cursor.fetchone()
                        if row:
                            curr_id = row["id"]
            except Exception as e:
                logger.error(f"Error resolving memory history query: {e}")

        # 1. Trace backward to root ancestor
        while curr_id and curr_id not in visited:
            visited.add(curr_id)
            rec = self.get_memory(curr_id)
            if not rec:
                break
            # Find what superseded curr_id or what curr_id superseded
            # Check who curr_id superseded
            try:
                with self.db.transaction() as conn:
                    cursor = conn.cursor()
                    cursor.execute("SELECT id FROM memories WHERE superseded_by = ?;", (curr_id,))
                    parent_row = cursor.fetchone()
                    if parent_row:
                        curr_id = parent_row["id"]
                    else:
                        break
            except Exception:
                break

        # 2. Trace forward from oldest ancestor
        root_id = curr_id or memory_id
        curr_id = root_id
        visited.clear()

        while curr_id and curr_id not in visited:
            visited.add(curr_id)
            rec = self.get_memory(curr_id)
            if rec:
                history.append(rec)
                curr_id = rec.superseded_by
            else:
                break

        return history

    def archive_memory(self, memory_id: str, superseded_by: str) -> bool:
        now = int(time.time())
        try:
            with self.db.transaction() as conn:
                cursor = conn.execute(
                    "UPDATE memories SET superseded_by = ?, updated_at = ? WHERE id = ?;",
                    (superseded_by, now, memory_id)
                )
                return cursor.rowcount > 0
        except Exception as e:
            logger.error(f"Error archiving memory '{memory_id}': {e}")
            return False

    def delete_memory(self, memory_id: str) -> bool:
        try:
            with self.db.transaction() as conn:
                cursor = conn.execute("DELETE FROM memories WHERE id = ?;", (memory_id,))
                return cursor.rowcount > 0
        except Exception as e:
            logger.error(f"Error deleting memory '{memory_id}': {e}")
            return False

    def prune_memories(
        self,
        threshold: float = MEMORY_PRUNE_THRESHOLD,
        min_age_days: int = MEMORY_PRUNE_MIN_AGE_DAYS,
        dry_run: bool = True
    ) -> dict:
        """
        Pruning Protocol (Section 6):
        1. Identifies superseded memories older than 90 days.
        2. Identifies active non-identity memories where effective_importance < threshold and access_count == 0 older than min_age_days.
        If dry_run is True, returns candidate list.
        If dry_run is False, removes them from SQLite.
        """
        now = int(time.time())
        min_age_seconds = min_age_days * 86400
        superseded_cutoff = now - (90 * 86400)

        prune_candidates = []

        try:
            with self.db.transaction() as conn:
                cursor = conn.cursor()

                # 1. Superseded memories past retention
                cursor.execute(
                    "SELECT * FROM memories WHERE superseded_by IS NOT NULL AND updated_at < ?;",
                    (superseded_cutoff,)
                )
                for row in cursor.fetchall():
                    rec = self._row_to_record(row)
                    prune_candidates.append({
                        "id": rec.id,
                        "content": rec.content,
                        "category": rec.category,
                        "reason": "superseded_retention_expired"
                    })

                # 2. Low-scoring active memories
                cursor.execute(
                    "SELECT * FROM memories WHERE superseded_by IS NULL AND category != 'identity' "
                    "AND access_count = 0 AND created_at < ?;",
                    (now - min_age_seconds,)
                )
                for row in cursor.fetchall():
                    rec = self._row_to_record(row)
                    eff_imp = compute_effective_importance(rec, current_time=now)
                    if eff_imp < threshold:
                        prune_candidates.append({
                            "id": rec.id,
                            "content": rec.content,
                            "category": rec.category,
                            "effective_importance": eff_imp,
                            "reason": "low_importance_decay"
                        })

                if not dry_run and prune_candidates:
                    ids_to_delete = [p["id"] for p in prune_candidates]
                    cursor.execute(
                        f"DELETE FROM memories WHERE id IN ({','.join(['?']*len(ids_to_delete))});",
                        tuple(ids_to_delete)
                    )
                    logger.info(f"Pruned {len(ids_to_delete)} memories from database.")

            return {
                "status": "success",
                "dry_run": dry_run,
                "pruned_count": len(prune_candidates),
                "candidates": prune_candidates
            }
        except Exception as e:
            logger.error(f"Error pruning memories: {e}")
            return {
                "status": "failure",
                "error": str(e),
                "dry_run": dry_run,
                "pruned_count": 0,
                "candidates": []
            }

    # -----------------------------------------------------------------
    # Hybrid Retrieval (Section 6 Specification)
    # -----------------------------------------------------------------

    async def retrieve_hybrid(
        self,
        query: str,
        category: str | None = None,
        scope: str = "global",
        include_global: bool = True,
        top_k: int = MEMORY_TOP_K,
        include_superseded: bool = False
    ) -> list[MemoryRecord]:
        """
        Executes hybrid retrieval:
        1. Query embedding generated on demand
        2. SQLite fetch of active memories matching scope/category
        3. Cosine similarity against stored BLOB embeddings
        4. Ranking: (effective_importance * 0.4) + (similarity * 0.4) + (recency_decay * 0.2)
        5. Returns ranked top-K records and updates access metrics
        """
        now = int(time.time())

        # 1. Compute query embedding vector
        q_vec = await self.lazy_embedder.embed_text(query)

        candidates: list[MemoryRecord] = []

        try:
            with self.db.transaction() as conn:
                cursor = conn.cursor()

                # Build filter query
                query_clauses = []
                params = []

                if not include_superseded:
                    query_clauses.append("superseded_by IS NULL")

                if scope == "global" or not include_global:
                    query_clauses.append("scope = ?")
                    params.append(scope)
                else:
                    query_clauses.append("(scope = ? OR scope = 'global')")
                    params.append(scope)

                if category:
                    query_clauses.append("category = ?")
                    params.append(category)

                where_stmt = f"WHERE {' AND '.join(query_clauses)}" if query_clauses else ""
                sql = f"SELECT * FROM memories {where_stmt};"
                cursor.execute(sql, tuple(params))
                rows = cursor.fetchall()
                candidates = [self._row_to_record(r) for r in rows]
        except Exception as e:
            logger.error(f"Error loading memory candidates: {e}")
            return []

        if not candidates:
            return []

        # 2. Compute Hybrid Scores with Effective Importance
        # Formula: (effective_importance * 0.4) + (semantic_similarity * 0.4) + (recency_decay * 0.2)
        for record in candidates:
            m_vec = unpack_vector(record.embedding) if record.embedding else []
            sim = cosine_similarity(q_vec, m_vec)

            # Recency decay over 30-day half-life
            time_diff = max(0, now - record.updated_at)
            recency_decay = math.exp(-time_diff / (30 * 86400))

            # Dynamic effective importance (with frequency boost & decay)
            effective_importance = compute_effective_importance(record, current_time=now)

            hybrid_score = (effective_importance * 0.4) + (sim * 0.4) + (recency_decay * 0.2)
            record.score = round(hybrid_score, 4)

        # 3. Sort descending by hybrid score and limit to top_k
        candidates.sort(key=lambda x: x.score, reverse=True)
        top_results = candidates[:top_k]

        # 4. Update access metrics in DB for returned memories
        try:
            with self.db.transaction() as conn:
                for rec in top_results:
                    conn.execute(
                        "UPDATE memories SET last_accessed_at = ?, access_count = access_count + 1 WHERE id = ?;",
                        (now, rec.id)
                    )
        except Exception as e:
            logger.error(f"Error updating memory access metrics: {e}")

        return top_results

    # -----------------------------------------------------------------
    # Router MemoryManager Implementation
    # -----------------------------------------------------------------

    async def query_memory(self, query: str, intent_data: dict) -> dict:
        """
        Entry point called by JarvisRouter for memory queries.
        Supports:
        1. Memory storing ("remember that ...", "save memory ...")
        2. Memory history/audit ("what was my previous ...", "memory history ...")
        3. Memory pruning ("prune memories ...", "clean up memory")
        4. Hybrid retrieval query
        """
        entities = intent_data.get("entities", {})
        intent = intent_data.get("intent", "memory_query")
        scope = entities.get("scope") or "global"
        category = entities.get("category") or None

        query_lower = query.lower().strip()

        # Check for Memory History query
        if "history" in query_lower or "previous" in query_lower or intent == "memory_history":
            target_mem_id = entities.get("memory_id")
            if not target_mem_id:
                # Retrieve closest memory first
                matches = await self.retrieve_hybrid(query, category=category, scope=scope, top_k=1, include_superseded=True)
                if matches:
                    target_mem_id = matches[0].id

            if target_mem_id:
                chain = self.get_memory_history(target_mem_id)
                bullets = []
                for idx, item in enumerate(chain, 1):
                    status = f"superseded by {item.superseded_by}" if item.superseded_by else "ACTIVE"
                    bullets.append(f"{idx}. {item.content} [{status}] (created: {time.strftime('%Y-%m-%d %H:%M', time.localtime(item.created_at))})")
                return {
                    "status": "success",
                    "response": f"Memory history for '{target_mem_id}':\n" + "\n".join(bullets),
                    "history": [
                        {"id": m.id, "content": m.content, "superseded_by": m.superseded_by, "created_at": m.created_at}
                        for m in chain
                    ],
                    "route": "memory"
                }

        # Check for Pruning command
        if ("prune" in query_lower and "memory" in query_lower) or intent == "prune_memories":
            dry_run = entities.get("dry_run", False)
            if isinstance(dry_run, str):
                dry_run = dry_run.lower() in ("true", "1", "yes")
            prune_res = self.prune_memories(dry_run=dry_run)
            cnt = prune_res.get("pruned_count", 0)
            mode_str = "Identified" if dry_run else "Pruned"
            return {
                "status": "success",
                "response": f"{mode_str} {cnt} decayed or superseded memories.",
                "prune_result": prune_res,
                "route": "memory"
            }

        # Check if user query is an explicit memory storage instruction
        store_match = re.search(r"\b(remember that|save memory|note that|remember)\s+(.*)", query, re.IGNORECASE)
        if store_match or intent == "store_memory":
            fact = store_match.group(2).strip() if store_match else query.strip()
            fact_lower = fact.lower()
            if any(k in fact_lower for k in ("like", "prefer", "favorite", "theme", "editor", "font")):
                cat = "preference"
            elif any(k in fact for k in ("i am", "my name", "my role", "my timezone", "my location")):
                cat = "identity"
            elif any(k in fact for k in ("goal", "target", "aim", "finish by")):
                cat = "goal"
            elif any(k in fact for k in ("project", "repo", "stack", "deploy", "branch")):
                cat = "project_context"
            else:
                cat = category or "preference"

            rec = await self.store_memory(
                content=fact,
                category=cat,
                scope=scope,
                source="explicit_statement"
            )

            superseded_msg = ""
            if rec.superseded_records:
                superseded_msg = f" (superseded {len(rec.superseded_records)} older fact(s))"

            return {
                "status": "success",
                "response": f"I've remembered: \"{fact}\" (Category: {cat}, Scope: {scope}){superseded_msg}.",
                "memory_id": rec.id,
                "superseded_ids": rec.superseded_records or [],
                "route": "memory"
            }

        # Otherwise perform hybrid retrieval
        memories = await self.retrieve_hybrid(query, category=category, scope=scope, top_k=MEMORY_TOP_K)

        if not memories:
            return {
                "status": "success",
                "response": "I couldn't find any relevant memories matching your query.",
                "memories": [],
                "route": "memory"
            }

        bullets = []
        for m in memories[:8]:
            bullets.append(f"• {m.content}")

        response_text = "\n".join(bullets)
        mem_list = [
            {
                "id": m.id,
                "content": m.content,
                "category": m.category,
                "scope": m.scope,
                "score": m.score
            }
            for m in memories
        ]
        return {
            "status": "success",
            "response": response_text,
            "memories": mem_list,
            "results": mem_list,
            "route": "memory"
        }

    # -----------------------------------------------------------------
    # Project-Specific Memory & Focus Context (Section 43)
    # -----------------------------------------------------------------

    def get_project_memories(self, project_id: str, limit: int = 5) -> list[MemoryRecord]:
        """
        Retrieves active project-scoped memories ranked by dynamic effective importance.
        """
        now = int(time.time())
        try:
            with self.db.transaction() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT * FROM memories WHERE scope = ? AND superseded_by IS NULL;",
                    (project_id,)
                )
                rows = cursor.fetchall()
                records = [self._row_to_record(r) for r in rows]

                # Compute dynamic effective importance for ranking
                for rec in records:
                    eff_imp = compute_effective_importance(rec, current_time=now)
                    rec.score = round(eff_imp, 4)

                records.sort(key=lambda r: r.score, reverse=True)
                return records[:limit]
        except Exception as e:
            logger.error(f"Error querying project memories for '{project_id}': {e}")
            return []

    def load_project_focus_context(self, project_id: str, context_store = None) -> list[MemoryRecord]:
        """
        Loads top-5 project-scoped memories into working context and updates current_focus in SQLite.
        """
        now = int(time.time())
        # 1. Update current_focus table in SQLite
        try:
            with self.db.transaction() as conn:
                conn.execute(
                    "INSERT INTO current_focus (id, project_id, updated_at) VALUES (1, ?, ?) "
                    "ON CONFLICT(id) DO UPDATE SET project_id = excluded.project_id, updated_at = excluded.updated_at;",
                    (project_id, now)
                )
        except Exception as e:
            logger.error(f"Error updating current_focus table for '{project_id}': {e}")

        # 2. Retrieve top project memories
        top_memories = self.get_project_memories(project_id, limit=5)

        # 3. Cache into active_context table if context_store is provided
        if context_store:
            try:
                mem_dicts = [
                    {
                        "id": m.id,
                        "content": m.content,
                        "category": m.category,
                        "importance": m.importance_score,
                        "effective_score": m.score
                    }
                    for m in top_memories
                ]
                context_store.set(f"project_focus_memories:{project_id}", json.dumps(mem_dicts))
                context_store.set("current_project_context", json.dumps({
                    "project_id": project_id,
                    "active_memories": mem_dicts,
                    "updated_at": now
                }))
                context_store.set("current_focus_project", project_id)
                logger.info(f"Loaded {len(top_memories)} project memories for '{project_id}' into working context.")
            except Exception as e:
                logger.error(f"Error writing project context to active_context: {e}")

        return top_memories

    def propose_project_memory(self, project_id: str, content: str, category: str = "project_context", context_store = None) -> dict:
        """
        Creates an opt-in proposal for remembering a project-specific fact (Section 43).
        """
        prompt = f"Want me to remember that project '{project_id}' uses '{content}'?"
        proposal = {
            "project_id": project_id,
            "content": content,
            "category": category,
            "prompt": prompt,
            "proposed_at": int(time.time())
        }
        if context_store:
            try:
                context_store.set("pending_project_memory_proposal", json.dumps(proposal))
            except Exception as e:
                logger.error(f"Error caching memory proposal: {e}")
        return proposal

    async def confirm_project_memory(self, confirm: bool = True, context_store = None) -> MemoryRecord | None:
        """
        Confirms or rejects a pending project memory proposal.
        """
        if not context_store:
            return None
        proposal_str = context_store.get("pending_project_memory_proposal")
        if not proposal_str:
            return None

        try:
            proposal = json.loads(proposal_str)
        except Exception:
            return None

        context_store.delete("pending_project_memory_proposal")

        if not confirm:
            logger.info("User declined project memory proposal.")
            return None

        rec = await self.store_memory(
            content=proposal["content"],
            category=proposal.get("category", "project_context"),
            scope=proposal["project_id"],
            source="explicit_statement"
        )
        # Refresh focus context
        self.load_project_focus_context(proposal["project_id"], context_store=context_store)
        return rec


# =====================================================================
# Dedicated Memory Tools
# =====================================================================

class StoreMemoryTool(Tool):
    """
    Stores a durable memory fact into SQLite with automatic conflict resolution,
    specificity analysis, and importance scoring.
    """

    def __init__(self, memory_manager: MemoryRepository):
        declaration = {
            "inputs": {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "The fact or statement to store"},
                    "category": {"type": "string", "description": "Category: identity, preference, goal, habit, decision, project_context"},
                    "scope": {"type": "string", "description": "Scope: 'global' or project_id"},
                    "importance_score": {"type": "number", "description": "Optional importance score (0.1 to 1.0)"},
                    "specificity_weight": {"type": "number", "description": "Optional specificity weight"}
                },
                "required": ["content"]
            },
            "side_effects": "Stores memory record into SQLite and archives any conflicting memories",
            "timeout_ms": 3000,
            "memory_limit_mb": 50
        }
        super().__init__("store_memory", "reversible", declaration)
        self.memory_manager = memory_manager

    async def execute(self, executor, **kwargs) -> dict:
        content = kwargs.get("content", "").strip()
        if not content:
            return {"status": "failure", "message": "Content parameter is required."}

        category = kwargs.get("category", "preference")
        scope = kwargs.get("scope", "global")
        importance = kwargs.get("importance_score")
        spec = kwargs.get("specificity_weight")

        rec = await self.memory_manager.store_memory(
            content=content,
            category=category,
            scope=scope,
            importance_score=float(importance) if importance is not None else None,
            specificity_weight=float(spec) if spec is not None else None,
            source="explicit_statement"
        )
        return {
            "status": "success",
            "response": f"Stored memory '{rec.id}' in category '{rec.category}'.",
            "memory_id": rec.id,
            "superseded_count": len(rec.superseded_records or []),
            "superseded_ids": rec.superseded_records or []
        }


class QueryMemoryTool(Tool):
    """
    Queries durable memories using hybrid semantic vector and structured search.
    """

    def __init__(self, memory_manager: MemoryRepository):
        declaration = {
            "inputs": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Query text to search for in memories"},
                    "category": {"type": "string", "description": "Optional category filter"},
                    "scope": {"type": "string", "description": "Scope filter, defaults to 'global'"},
                    "top_k": {"type": "integer", "description": "Number of results to retrieve"}
                },
                "required": ["query"]
            },
            "side_effects": "Reads memories from SQLite and updates last_accessed_at metrics",
            "timeout_ms": 3000,
            "memory_limit_mb": 50
        }
        super().__init__("query_memory", "reversible", declaration)
        self.memory_manager = memory_manager

    async def execute(self, executor, **kwargs) -> dict:
        q = (kwargs.get("query") or kwargs.get("topic") or "").strip()
        cat = kwargs.get("category")
        sc = kwargs.get("scope", "global")
        k = int(kwargs.get("top_k", MEMORY_TOP_K))

        results = await self.memory_manager.retrieve_hybrid(q, category=cat, scope=sc, top_k=k)
        items = [
            {
                "id": m.id,
                "content": m.content,
                "category": m.category,
                "score": m.score
            }
            for m in results
        ]
        resp_text = f"Found {len(items)} memories matching '{q}':\n" + "\n".join(f"- [{m['category']}] {m['content']}" for m in items) if items else f"No memories found matching '{q}'."
        return {
            "status": "success",
            "count": len(results),
            "response": resp_text,
            "memories": items
        }


class MemoryHistoryTool(Tool):
    """
    Retrieves the chronological audit history and lineage chain of a superseded fact.
    """

    def __init__(self, memory_manager: MemoryRepository):
        declaration = {
            "inputs": {
                "type": "object",
                "properties": {
                    "memory_id": {"type": "string", "description": "Memory ID to trace history for"}
                },
                "required": ["memory_id"]
            },
            "side_effects": "Reads memory history chain from SQLite",
            "timeout_ms": 3000,
            "memory_limit_mb": 50
        }
        super().__init__("memory_history", "reversible", declaration)
        self.memory_manager = memory_manager

    async def execute(self, executor, **kwargs) -> dict:
        m_id = (kwargs.get("memory_id") or kwargs.get("topic") or kwargs.get("query") or "").strip()
        if not m_id:
            return {"status": "failure", "message": "memory_id parameter is required."}

        chain = self.memory_manager.get_memory_history(m_id)
        entries = [
            f"[{'ACTIVE' if not m.superseded_by else 'SUPERSEDED'}] {m.content}"
            for m in chain
        ]
        resp_text = f"Memory History Lineage ({len(chain)} revisions):\n" + "\n".join(f"  - {e}" for e in entries) if chain else f"No memory history found for '{m_id}'."
        return {
            "status": "success",
            "memory_id": m_id,
            "response": resp_text,
            "history_count": len(chain),
            "history": [
                {
                    "id": m.id,
                    "content": m.content,
                    "category": m.category,
                    "superseded_by": m.superseded_by,
                    "created_at": m.created_at
                }
                for m in chain
            ]
        }


class PruneMemoriesTool(Tool):
    """
    Executes memory maintenance pruning for expired superseded records and decayed facts.
    """

    def __init__(self, memory_manager: MemoryRepository):
        declaration = {
            "inputs": {
                "type": "object",
                "properties": {
                    "dry_run": {"type": "boolean", "description": "Whether to perform a dry-run preview"},
                    "threshold": {"type": "number", "description": "Effective importance threshold (default 0.15)"}
                }
            },
            "side_effects": "Deletes decayed and expired superseded memories when dry_run=False",
            "timeout_ms": 5000,
            "memory_limit_mb": 50
        }
        super().__init__("prune_memories", "destructive", declaration)
        self.memory_manager = memory_manager

    async def execute(self, executor, **kwargs) -> dict:
        dry = kwargs.get("dry_run", True)
        if isinstance(dry, str):
            dry = dry.lower() in ("true", "1", "yes")
        th = kwargs.get("threshold", MEMORY_PRUNE_THRESHOLD)
        res = self.memory_manager.prune_memories(threshold=float(th), dry_run=dry)
        mode_str = "Dry Run" if dry else "Executed"
        count = res.get("pruned_count", 0)
        res["response"] = f"Memory Pruning Report ({mode_str}): {count} candidate memories {'identified for removal' if dry else 'successfully pruned'}."
        return res


class StoreProjectMemoryTool(Tool):
    """
    Stores a durable memory fact scoped to a specific project (Section 43).
    """

    def __init__(self, memory_manager: MemoryRepository, context_store = None):
        declaration = {
            "inputs": {
                "type": "object",
                "properties": {
                    "project_id": {"type": "string", "description": "Target project ID or folder name"},
                    "content": {"type": "string", "description": "The fact, convention, or note to remember for this project"},
                    "category": {"type": "string", "default": "project_context", "description": "Category (project_context, preference, decision, blocker)"}
                },
                "required": ["project_id", "content"]
            },
            "side_effects": "Creates or updates project-scoped memory record",
            "timeout_ms": 3000,
            "memory_limit_mb": 50
        }
        super().__init__("store_project_memory", "creates_file", declaration)
        self.memory_manager = memory_manager
        self.context_store = context_store

    async def execute(self, executor, **kwargs) -> dict:
        project_id = kwargs.get("project_id", "").strip()
        content = kwargs.get("content", "").strip()
        category = kwargs.get("category", "project_context").strip()

        if not project_id or not content:
            return {"status": "failure", "message": "Missing required 'project_id' or 'content' argument."}

        rec = await self.memory_manager.store_memory(
            content=content,
            category=category,
            scope=project_id,
            source="explicit_statement"
        )

        # If current focus is this project, refresh context
        c_store = self.context_store or getattr(executor, "context_store", None)
        if c_store:
            self.memory_manager.load_project_focus_context(project_id, context_store=c_store)

        return {
            "status": "success",
            "response": f"Remembered for project '{project_id}': \"{content}\" (Category: {category}).",
            "memory_id": rec.id,
            "project_id": project_id
        }


class ListProjectMemoriesTool(Tool):
    """
    Lists active memories scoped to a specific project.
    """

    def __init__(self, memory_manager: MemoryRepository):
        declaration = {
            "inputs": {
                "type": "object",
                "properties": {
                    "project_id": {"type": "string", "description": "Target project ID or name"},
                    "limit": {"type": "integer", "default": 5, "description": "Maximum number of memories to return"}
                },
                "required": ["project_id"]
            },
            "side_effects": "none",
            "timeout_ms": 3000,
            "memory_limit_mb": 50
        }
        super().__init__("list_project_memories", "read_only", declaration)
        self.memory_manager = memory_manager

    async def execute(self, executor, **kwargs) -> dict:
        project_id = kwargs.get("project_id", "").strip()
        limit = int(kwargs.get("limit", 5))

        if not project_id:
            return {"status": "failure", "message": "Missing required 'project_id' parameter."}

        memories = self.memory_manager.get_project_memories(project_id, limit=limit)
        if not memories:
            return {
                "status": "success",
                "response": f"No project-specific memories found for project '{project_id}'.",
                "project_id": project_id,
                "memories": []
            }

        lines = [f"Project Memories for '{project_id}':"]
        for idx, m in enumerate(memories, 1):
            lines.append(f"{idx}. {m.content} (Category: {m.category}, Importance: {m.score})")

        return {
            "status": "success",
            "response": "\n".join(lines),
            "project_id": project_id,
            "memories": [
                {
                    "id": m.id,
                    "content": m.content,
                    "category": m.category,
                    "importance": m.importance_score,
                    "score": m.score
                }
                for m in memories
            ]
        }


class SwitchProjectFocusTool(Tool):
    """
    Switches active project focus and pre-loads project-specific memories into working context (Section 43 & 44).
    """

    def __init__(self, memory_manager: MemoryRepository, context_store = None):
        declaration = {
            "inputs": {
                "type": "object",
                "properties": {
                    "project_id": {"type": "string", "description": "Target project ID or name"}
                },
                "required": ["project_id"]
            },
            "side_effects": "Updates current_focus and active_context working memory",
            "timeout_ms": 3000,
            "memory_limit_mb": 50
        }
        super().__init__("switch_project_focus", "reversible", declaration)
        self.memory_manager = memory_manager
        self.context_store = context_store

    async def execute(self, executor, **kwargs) -> dict:
        project_id = kwargs.get("project_id", "").strip()
        if not project_id:
            return {"status": "failure", "message": "Missing required 'project_id' parameter."}

        c_store = self.context_store or getattr(executor, "context_store", None)
        loaded = self.memory_manager.load_project_focus_context(project_id, context_store=c_store)

        notes_count = len(loaded)
        if notes_count > 0:
            preview = ", ".join([f'"{m.content[:35]}..."' for m in loaded[:2]])
            msg = f"Switched focus to project '{project_id}'. Loaded {notes_count} project memories into working context ({preview})."
        else:
            msg = f"Switched focus to project '{project_id}'. No existing project memories found."

        return {
            "status": "success",
            "response": msg,
            "project_id": project_id,
            "memories_loaded_count": notes_count
        }
