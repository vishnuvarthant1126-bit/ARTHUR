"""Vector store: saves embeddings and finds the nearest ones to a query.

"Nearest" is measured with *cosine similarity*: 1.0 means pointing in the
same direction (same meaning), ~0 means unrelated. A vector database does
this search quickly even for millions of items.

`VectorStore` is an interface so ChromaDB can be swapped (FAISS, Qdrant...)
without touching the memory code. `InMemoryVectorStore` is a tiny pure-Python
version used by tests.
"""

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path


@dataclass
class VectorHit:
    id: str
    score: float  # cosine similarity, higher = more similar


class VectorStore(ABC):
    @abstractmethod
    def upsert(self, id: str, embedding: list[float], text: str) -> None:
        """Insert, or replace if the id already exists."""

    @abstractmethod
    def query(self, embedding: list[float], k: int) -> list[VectorHit]:
        """Return up to k nearest items, most similar first."""

    @abstractmethod
    def delete(self, id: str) -> None: ...

    @abstractmethod
    def count(self) -> int: ...


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

    def upsert(self, id: str, embedding: list[float], text: str) -> None:
        self._collection.upsert(ids=[id], embeddings=[embedding], documents=[text])

    def query(self, embedding: list[float], k: int) -> list[VectorHit]:
        total = self.count()
        if total == 0:
            return []
        result = self._collection.query(query_embeddings=[embedding], n_results=min(k, total))
        # Chroma returns cosine *distance* (0 = identical); similarity = 1 - distance.
        return [
            VectorHit(id=id_, score=round(1 - distance, 4))
            for id_, distance in zip(result["ids"][0], result["distances"][0], strict=True)
        ]

    def delete(self, id: str) -> None:
        self._collection.delete(ids=[id])

    def count(self) -> int:
        return self._collection.count()


class InMemoryVectorStore(VectorStore):
    def __init__(self) -> None:
        self._items: dict[str, list[float]] = {}

    def upsert(self, id: str, embedding: list[float], text: str) -> None:
        self._items[id] = embedding

    def query(self, embedding: list[float], k: int) -> list[VectorHit]:
        hits = [VectorHit(id=i, score=cosine(embedding, e)) for i, e in self._items.items()]
        return sorted(hits, key=lambda h: h.score, reverse=True)[:k]

    def delete(self, id: str) -> None:
        self._items.pop(id, None)

    def count(self) -> int:
        return len(self._items)


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return round(dot / norm, 4) if norm else 0.0
