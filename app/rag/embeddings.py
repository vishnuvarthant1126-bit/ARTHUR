"""Embeddings: turning text into numbers that capture its meaning.

An embedding model reads a sentence and outputs a list of numbers (768 for
nomic-embed-text) - think of it as coordinates on a "map of meaning".
Sentences that mean similar things land close together on that map:

    "I love Python"              -> [0.12, -0.40, 0.88, ...]
    "My favourite language is Python" -> [0.10, -0.38, 0.85, ...]   (close!)
    "It is raining in Singapore" -> [-0.70, 0.22, 0.05, ...]   (far away)

So we can find memories by *meaning*, even when no words match exactly.
"""

import hashlib
import re
from abc import ABC, abstractmethod

import httpx

from app.llm._http import KEEP_ALIVE, status_error, translate_http_errors
from app.llm.base import LLMResponseError


class EmbeddingProvider(ABC):
    model: str

    @abstractmethod
    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Embed texts that will be stored and searched later."""

    @abstractmethod
    async def embed_query(self, text: str) -> list[float]:
        """Embed a search query."""

    async def aclose(self) -> None:  # noqa: B027 - optional hook
        pass


class OllamaEmbeddings(EmbeddingProvider):
    # nomic-embed-text is trained with these task prefixes; using them
    # measurably improves search quality.
    DOCUMENT_PREFIX = "search_document: "
    QUERY_PREFIX = "search_query: "

    def __init__(
        self,
        base_url: str,
        model: str,
        client: httpx.AsyncClient | None = None,
        keep_alive: str | None = None,
    ) -> None:
        self.model = model
        self._base_url = base_url
        # How long Ollama keeps the embedding model loaded. Its own default is 5 minutes,
        # after which the next question waits ~2 s for a reload (measured in Phase 24).
        self._keep_alive = keep_alive
        self._client = client or httpx.AsyncClient(
            base_url=base_url, timeout=httpx.Timeout(60.0, connect=5.0), limits=KEEP_ALIVE
        )
        self._use_prefixes = model.startswith("nomic-embed")

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        prefix = self.DOCUMENT_PREFIX if self._use_prefixes else ""
        return await self._embed([prefix + t for t in texts])

    async def embed_query(self, text: str) -> list[float]:
        prefix = self.QUERY_PREFIX if self._use_prefixes else ""
        return (await self._embed([prefix + text]))[0]

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _embed(self, inputs: list[str]) -> list[list[float]]:
        payload: dict = {"model": self.model, "input": inputs}
        if self._keep_alive:
            payload["keep_alive"] = self._keep_alive
        with translate_http_errors("Ollama embeddings", self._base_url):
            response = await self._client.post("/api/embed", json=payload)
        if response.status_code == 404:
            raise LLMResponseError(
                f"Embedding model '{self.model}' is not installed. Run: ollama pull {self.model}"
            )
        if response.status_code >= 400:
            raise status_error("Ollama embeddings", response)
        embeddings = response.json().get("embeddings")
        if not embeddings or len(embeddings) != len(inputs):
            raise LLMResponseError("Ollama returned no embeddings.")
        return embeddings


class HashEmbeddings(EmbeddingProvider):
    """Embeddings without a model (EMBEDDING_PROVIDER=hash) - for load tests and offline demos.

    Each word is hashed to one of 256 positions; texts sharing words get similar vectors.
    It matches WORDS, not meaning - fine for measuring speed, useless for real search quality.
    """

    DIMENSIONS = 256
    model = "hash"

    def _vector(self, text: str) -> list[float]:
        vector = [0.0] * self.DIMENSIONS
        for word in re.findall(r"[a-z0-9']+", text.lower()):
            vector[int(hashlib.md5(word.encode()).hexdigest(), 16) % self.DIMENSIONS] += 1.0  # noqa: S324
        return vector if any(vector) else [1e-6] * self.DIMENSIONS

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    async def embed_query(self, text: str) -> list[float]:
        return self._vector(text)


def create_embedding_provider(
    name: str, base_url: str, model: str, keep_alive: str | None = None
) -> EmbeddingProvider:
    match name.lower():
        case "ollama":
            return OllamaEmbeddings(base_url=base_url, model=model, keep_alive=keep_alive)
        case "hash":
            return HashEmbeddings()
        case other:
            raise ValueError(f"Unknown EMBEDDING_PROVIDER '{other}'. Supported: ollama, hash")
