"""Step 4 of RAG: find the passages most relevant to a question - with their sources."""

import asyncio
import time

from pydantic import BaseModel

from app.memory.vector_store import VectorStore
from app.observability.logging import get_logger
from app.rag.embeddings import EmbeddingProvider

log = get_logger(__name__)


class DocumentHit(BaseModel):
    doc_id: str
    filename: str
    page_label: str | None
    chunk: int
    text: str
    score: float

    @property
    def citation(self) -> str:
        """How to cite this passage, e.g. 'handbook.pdf, p. 2'."""
        return f"{self.filename}, {self.page_label}" if self.page_label else self.filename


class DocumentRetriever:
    def __init__(self, vectors: VectorStore, embeddings: EmbeddingProvider) -> None:
        self.vectors = vectors
        self.embeddings = embeddings

    async def search(self, query: str, k: int = 5, min_score: float = 0.58) -> list[DocumentHit]:
        start = time.perf_counter()
        embedding = await self.embeddings.embed_query(query)
        raw = await asyncio.to_thread(self.vectors.query, embedding, k)
        hits = [
            DocumentHit(
                doc_id=str(h.metadata.get("doc_id", "")),
                filename=str(h.metadata.get("filename", "unknown")),
                page_label=str(h.metadata["page_label"]) if h.metadata.get("page_label") else None,
                chunk=int(h.metadata.get("chunk", 0)),
                text=h.text,
                score=h.score,
            )
            for h in raw
            if h.score >= min_score
        ]
        log.info(
            "document_search",
            hits=len(hits),
            candidates=len(raw),
            top_score=raw[0].score if raw else None,
            duration_ms=round((time.perf_counter() - start) * 1000, 1),
        )
        return hits
