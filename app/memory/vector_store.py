"""Vector store: saves embeddings and finds the nearest ones to a query.

"Nearest" is measured with *cosine similarity*: 1.0 means pointing in the
same direction (same meaning), ~0 means unrelated. A vector database does
this search quickly even for millions of items.

Each item can carry its text and *metadata* (e.g. which file and page a
document chunk came from) so search results can be cited.

`VectorStore` is an interface so ChromaDB can be swapped (FAISS, Qdrant...)
without touching the memory or RAG code. `InMemoryVectorStore` is a tiny
pure-Python version used by tests.
"""

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

Metadata = dict[str, str | int | float | bool]


@dataclass
class VectorItem:
    id: str
    embedding: list[float]
    text: str
    metadata: Metadata = field(default_factory=dict)


@dataclass
class VectorHit:
    id: str
    score: float  # cosine similarity, higher = more similar
    text: str = ""
    metadata: Metadata = field(default_factory=dict)


class VectorStore(ABC):
    @abstractmethod
    def upsert_many(self, items: list[VectorItem]) -> None:
        """Insert, or replace items whose id already exists."""

    @abstractmethod
    def query(self, embedding: list[float], k: int) -> list[VectorHit]:
        """Return up to k nearest items, most similar first."""

    @abstractmethod
    def delete(self, id: str) -> None: ...

    @abstractmethod
    def delete_where(self, key: str, value: Any) -> None:
        """Delete every item whose metadata[key] == value (e.g. all chunks of one file)."""

    @abstractmethod
    def count(self) -> int: ...

    def upsert(
        self, id: str, embedding: list[float], text: str, metadata: Metadata | None = None
    ) -> None:
        self.upsert_many([VectorItem(id, embedding, text, metadata or {})])


class ChromaVectorStore(VectorStore):
    def __init__(self, path: Path, collection: str = "memories") -> None:
        import chromadb  # imported here so tests without Chroma stay fast
        from chromadb.config import Settings as ChromaSettings

        path.mkdir(parents=True, exist_ok=True)
        self._client = chromadb.PersistentClient(
            path=str(path),
            # Privacy: never send usage statistics anywhere.
            settings=ChromaSettings(anonymized_telemetry=False),
        )
        self._collection = self._client.get_or_create_collection(
            name=collection,
            embedding_function=None,  # we always pass our own embeddings
            metadata={"hnsw:space": "cosine"},
        )

    def upsert_many(self, items: list[VectorItem]) -> None:
        if not items:
            return
        self._collection.upsert(
            ids=[i.id for i in items],
            embeddings=[i.embedding for i in items],
            documents=[i.text for i in items],
            # Chroma rejects empty metadata dicts; None means "no metadata".
            metadatas=[i.metadata or None for i in items],
        )

    def query(self, embedding: list[float], k: int) -> list[VectorHit]:
        total = self.count()
        if total == 0:
            return []
        result = self._collection.query(
            query_embeddings=[embedding],
            n_results=min(k, total),
            include=["distances", "documents", "metadatas"],
        )
        # Chroma returns cosine *distance* (0 = identical); similarity = 1 - distance.
        return [
            VectorHit(id=id_, score=round(1 - distance, 4), text=text or "", metadata=meta or {})
            for id_, distance, text, meta in zip(
                result["ids"][0],
                result["distances"][0],
                result["documents"][0],
                result["metadatas"][0],
                strict=True,
            )
        ]

    def delete(self, id: str) -> None:
        self._collection.delete(ids=[id])

    def delete_where(self, key: str, value: Any) -> None:
        self._collection.delete(where={key: value})

    def count(self) -> int:
        return self._collection.count()


class InMemoryVectorStore(VectorStore):
    def __init__(self) -> None:
        self._items: dict[str, VectorItem] = {}

    def upsert_many(self, items: list[VectorItem]) -> None:
        for item in items:
            self._items[item.id] = item

    def query(self, embedding: list[float], k: int) -> list[VectorHit]:
        hits = [
            VectorHit(i.id, cosine(embedding, i.embedding), i.text, dict(i.metadata))
            for i in self._items.values()
        ]
        return sorted(hits, key=lambda h: h.score, reverse=True)[:k]

    def delete(self, id: str) -> None:
        self._items.pop(id, None)

    def delete_where(self, key: str, value: Any) -> None:
        for id_ in [i.id for i in self._items.values() if i.metadata.get(key) == value]:
            del self._items[id_]

    def count(self) -> int:
        return len(self._items)


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return round(dot / norm, 4) if norm else 0.0
