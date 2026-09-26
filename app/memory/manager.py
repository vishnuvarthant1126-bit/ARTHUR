"""MemoryManager: the one entry point for long-term memory.

    save_memory()      store a fact (SQLite row + embedding in the vector store)
    retrieve_memory()  fetch one memory by id
    search_memory()    find memories by meaning
    delete_memory()    remove from both stores
    list_memories()    everything, newest first

SQLite and the vector store are separate systems, so every write keeps them
in step: if the second write fails, the first is undone.
"""

import asyncio
import time

from pydantic import BaseModel

from app.memory.long_term import Memory, MemoryRepository
from app.memory.vector_store import VectorStore
from app.observability.logging import get_logger
from app.rag.embeddings import EmbeddingProvider

log = get_logger(__name__)

MAX_MEMORY_CHARS = 1000
CATEGORIES = ("preference", "project", "personal", "task", "other")


class MemorySearchResult(BaseModel):
    memory: Memory
    score: float


class MemoryManager:
    def __init__(
        self,
        repository: MemoryRepository,
        vectors: VectorStore,
        embeddings: EmbeddingProvider,
        *,
        duplicate_threshold: float = 0.95,
    ) -> None:
        self.repository = repository
        self.vectors = vectors
        self.embeddings = embeddings
        # Saving something almost identical to an existing memory updates it instead.
        self.duplicate_threshold = duplicate_threshold

    async def save_memory(
        self, content: str, category: str = "other", source: str = "chat"
    ) -> tuple[Memory, bool]:
        """Returns (memory, created). created=False means an existing memory was updated."""
        content = content.strip()
        if not content:
            raise ValueError("Memory content must not be empty.")
        if len(content) > MAX_MEMORY_CHARS:
            raise ValueError(f"Memory is too long (max {MAX_MEMORY_CHARS} characters).")
        category = category if category in CATEGORIES else "other"

        [embedding] = await self.embeddings.embed_documents([content])

        nearest = await asyncio.to_thread(self.vectors.query, embedding, 1)
        if nearest and nearest[0].score >= self.duplicate_threshold:
            updated = await asyncio.to_thread(
                self.repository.update, nearest[0].id, content, category
            )
            if updated:
                await asyncio.to_thread(self.vectors.upsert, updated.id, embedding, content)
                log.info("memory_updated", memory_id=updated.id, similarity=nearest[0].score)
                return updated, False

        memory = await asyncio.to_thread(self.repository.add, content, category, source)
        try:
            await asyncio.to_thread(self.vectors.upsert, memory.id, embedding, content)
        except Exception:
            await asyncio.to_thread(self.repository.delete, memory.id)  # keep stores in step
            raise
        log.info("memory_saved", memory_id=memory.id, category=category, source=source)
        return memory, True

    async def retrieve_memory(self, memory_id: str) -> Memory | None:
        return await asyncio.to_thread(self.repository.get, memory_id)

    async def list_memories(self) -> list[Memory]:
        return await asyncio.to_thread(self.repository.list_all)

    async def delete_memory(self, memory_id: str) -> bool:
        deleted = await asyncio.to_thread(self.repository.delete, memory_id)
        await asyncio.to_thread(self.vectors.delete, memory_id)
        if deleted:
            log.info("memory_deleted", memory_id=memory_id)
        return deleted

    async def search_memory(
        self, query: str, k: int = 5, min_score: float = 0.5
    ) -> list[MemorySearchResult]:
        start = time.perf_counter()
        embedding = await self.embeddings.embed_query(query)
        hits = await asyncio.to_thread(self.vectors.query, embedding, k)
        relevant = [h for h in hits if h.score >= min_score]
        records = await asyncio.to_thread(self.repository.get_many, [h.id for h in relevant])
        results = [
            MemorySearchResult(memory=records[h.id], score=h.score)
            for h in relevant
            if h.id in records  # skip orphans (vector without a row)
        ]
        log.info(
            "memory_search",
            hits=len(results),
            candidates=len(hits),
            top_score=hits[0].score if hits else None,
            duration_ms=round((time.perf_counter() - start) * 1000, 1),
        )
        return results

    async def count(self) -> int:
        return await asyncio.to_thread(self.repository.count)
