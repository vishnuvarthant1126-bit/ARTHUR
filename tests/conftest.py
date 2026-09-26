"""Shared test fixtures.

`FakeLLM` implements the same `LLMProvider` interface as Ollama but returns
canned answers instantly - so API tests are fast, free and deterministic.
`FakeEmbeddings` does the same for the embedding model.
"""

import asyncio
import hashlib
import re
from collections.abc import AsyncIterator, Iterator

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config.settings import Settings
from app.database.database import Database
from app.llm.base import LLMError, LLMProvider, LLMResponse, LLMResponseError, Message, T
from app.main import build_orchestrator, create_app
from app.memory.long_term import MemoryRepository
from app.memory.manager import MemoryManager
from app.memory.vector_store import InMemoryVectorStore
from app.observability.logging import configure_logging
from app.rag.embeddings import EmbeddingProvider
from app.security.audit import AuditLog
from app.security.permissions import PermissionPolicy
from app.tools.defaults import create_tool_registry


class FakeLLM(LLMProvider):
    name = "fake"
    model = "fake-model"

    def __init__(
        self,
        reply: str = "Hello. How can I help?",
        error: LLMError | None = None,
        token_delay: float = 0.0,
        structured_reply: str | None = None,
    ):
        self.reply = reply
        self.error = error
        self.token_delay = token_delay  # seconds between streamed tokens
        self.structured_reply = structured_reply  # JSON returned by generate_structured
        self.calls: list[list[Message]] = []

    async def generate(self, messages, *, temperature=None) -> LLMResponse:
        self.calls.append(messages)
        if self.error:
            raise self.error
        return LLMResponse(content=self.reply, model=self.model, latency_ms=1.0)

    async def stream(self, messages, *, temperature=None) -> AsyncIterator[str]:
        self.calls.append(messages)
        if self.error:
            raise self.error
        words = self.reply.split(" ")
        for i, word in enumerate(words):
            await asyncio.sleep(self.token_delay)
            yield word if i == len(words) - 1 else word + " "

    async def generate_structured(self, messages, schema: type[T]) -> T:
        self.calls.append(messages)
        if self.error:
            raise self.error
        try:
            return schema.model_validate_json(self.structured_reply or self.reply)
        except ValidationError as exc:
            raise LLMResponseError(f"did not match {schema.__name__}") from exc


class FakeEmbeddings(EmbeddingProvider):
    """Bag-of-words vectors: texts sharing words are 'similar'. Deterministic and instant."""

    model = "fake-embed"
    DIMENSIONS = 4096  # large, so two different words rarely share a slot
    STOPWORDS = {"the", "a", "an", "is", "my", "user", "user's", "that", "to", "of", "what"}

    def __init__(self) -> None:
        self.calls = 0

    def _vector(self, text: str) -> list[float]:
        vector = [0.0] * self.DIMENSIONS
        for word in re.findall(r"[a-z0-9']+", text.lower()):
            if word not in self.STOPWORDS:
                index = int(hashlib.md5(word.encode()).hexdigest(), 16) % self.DIMENSIONS
                vector[index] += 1.0
        return vector if any(vector) else [1e-6] * self.DIMENSIONS

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        return [self._vector(t) for t in texts]

    async def embed_query(self, text: str) -> list[float]:
        self.calls += 1
        return self._vector(text)


def make_memory() -> tuple[MemoryManager, Database]:
    db = Database(":memory:")
    db.create_tables()
    return MemoryManager(MemoryRepository(db), InMemoryVectorStore(), FakeEmbeddings()), db


def offline_http_client() -> httpx.AsyncClient:
    """HTTP client whose every request fails - tests must never touch the internet."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("tests are offline", request=request)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


TEST_SETTINGS = Settings(_env_file=None, memory_min_score=0.3)


@pytest.fixture
def fake_llm() -> FakeLLM:
    return FakeLLM()


@pytest.fixture
def memory() -> MemoryManager:
    manager, _ = make_memory()
    return manager


def _app_with(llm: LLMProvider):
    configure_logging("WARNING")
    app = create_app()
    memory_manager, db = make_memory()
    audit = AuditLog(db)
    # Inject fakes instead of running the real lifespan.
    app.state.llm = llm
    app.state.memory = memory_manager
    app.state.audit = audit
    app.state.tools = create_tool_registry(
        policy=PermissionPolicy(),
        audit=audit,
        http_client=offline_http_client(),
        memory=memory_manager,
        memory_min_score=TEST_SETTINGS.memory_min_score,
    )
    app.state.orchestrator = build_orchestrator(llm, TEST_SETTINGS, memory_manager)
    return app


@pytest.fixture
async def client(fake_llm: FakeLLM) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=_app_with(fake_llm), raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def ws_client(fake_llm: FakeLLM) -> Iterator[TestClient]:
    """Synchronous client that can open WebSocket connections.

    Not used as a context manager on purpose: that would run the real lifespan,
    replacing the fakes with real Ollama/Chroma/SQLite.
    """
    yield TestClient(_app_with(fake_llm))
