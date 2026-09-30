"""Shared test fixtures.

`FakeLLM` implements the same `LLMProvider` interface as Ollama but returns
canned answers instantly - so API tests are fast, free and deterministic.
`FakeEmbeddings` does the same for the embedding model.
"""

import asyncio
import hashlib
import re
import tempfile
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config.settings import Settings
from app.database.database import Database
from app.llm.base import (
    LLMError,
    LLMProvider,
    LLMResponse,
    LLMResponseError,
    Message,
    T,
    TextDelta,
    ToolCall,
    ToolCallsRequested,
)
from app.main import build_orchestrator, create_app
from app.memory.long_term import MemoryRepository
from app.memory.manager import MemoryManager
from app.memory.vector_store import InMemoryVectorStore
from app.observability.logging import configure_logging
from app.rag.documents import DocumentService
from app.rag.embeddings import EmbeddingProvider
from app.rag.retrieval import DocumentRetriever
from app.search.base import SearchProvider, SearchResult
from app.search.service import WebSearchService
from app.security.audit import AuditLog
from app.security.permissions import PermissionPolicy
from app.tools.defaults import create_tool_registry
from app.vision.provider import VisionProvider
from app.voice.speech_to_text import SpeechToText, Transcript


def tool_call(name: str, **arguments) -> ToolCall:
    """Shorthand for tests: a model request to run a tool."""
    return ToolCall(id=f"call_{name}", name=name, arguments=arguments)


class FakeLLM(LLMProvider):
    """A pretend model.

    `script` makes it behave like an agent: each chat turn uses the next entry -
    a list of ToolCalls ("I want these tools") or a string (a final answer).
    When the script runs out it answers with `reply`.
    """

    name = "fake"
    model = "fake-model"

    def __init__(
        self,
        reply: str = "Hello. How can I help?",
        error: LLMError | None = None,
        token_delay: float = 0.0,
        structured_reply: str | None = None,
        script: list[str | list[ToolCall] | Exception] | None = None,
    ):
        self.reply = reply
        self.error = error
        self.token_delay = token_delay  # seconds between streamed tokens
        self.structured_reply = structured_reply  # JSON returned by generate_structured
        self.script = list(script or [])
        self.calls: list[list[Message]] = []
        self.tools_offered: list[list[dict] | None] = []

    def _next_turn(self) -> str | list[ToolCall]:
        turn = self.script.pop(0) if self.script else self.reply
        if isinstance(turn, Exception):  # scripted failure, e.g. LLMUnavailableError
            raise turn
        return turn

    async def generate(self, messages, *, temperature=None, tools=None) -> LLMResponse:
        self.calls.append(list(messages))
        self.tools_offered.append(tools)
        if self.error:
            raise self.error
        turn = self._next_turn()
        if isinstance(turn, list):
            return LLMResponse(content="", model=self.model, latency_ms=1.0, tool_calls=turn)
        return LLMResponse(content=turn, model=self.model, latency_ms=1.0)

    async def stream(self, messages, *, temperature=None) -> AsyncIterator[str]:
        self.calls.append(list(messages))
        if self.error:
            raise self.error
        async for word in self._words(self.reply):
            yield word

    async def stream_chat(self, messages, *, tools=None, temperature=None):
        self.calls.append(list(messages))
        self.tools_offered.append(tools)
        if self.error:
            raise self.error
        turn = self._next_turn()
        if isinstance(turn, list):
            yield ToolCallsRequested(calls=turn)
            return
        async for word in self._words(turn):
            yield TextDelta(text=word)

    async def _words(self, text: str) -> AsyncIterator[str]:
        words = text.split(" ")
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


def make_pdf(*pages: str) -> bytes:
    """A small real PDF with one text page per argument."""
    from fpdf import FPDF

    pdf = FPDF()
    for text in pages:
        pdf.add_page()
        pdf.set_font("Helvetica", size=11)
        pdf.multi_cell(0, 6, text)
    return bytes(pdf.output())


def make_documents(db: Database, storage_dir: Path) -> tuple[DocumentService, DocumentRetriever]:
    vectors, embeddings = InMemoryVectorStore(), FakeEmbeddings()
    service = DocumentService(
        db, vectors, embeddings, storage_dir, chunk_size=300, chunk_overlap=50
    )
    return service, DocumentRetriever(vectors, embeddings)


class FakeSearchProvider(SearchProvider):
    """Pretend search engine: returns `results`, or raises the next error in `errors`."""

    name = "fake-search"

    def __init__(self, results: list[SearchResult] | None = None, errors=None) -> None:
        self.results = (
            results
            if results is not None
            else [
                SearchResult(
                    title="Python Release Python 3.14.7",
                    url="https://www.python.org/downloads/latest/",
                    snippet="The latest version of Python is 3.14.7.",
                )
            ]
        )
        self.errors = list(errors or [])
        self.queries: list[str] = []

    async def search(self, query: str, max_results: int) -> list[SearchResult]:
        self.queries.append(query)
        if self.errors:
            raise self.errors.pop(0)
        return self.results[:max_results]


class FakeSTT(SpeechToText):
    """Pretend speech recogniser: returns `text`, or raises `error`."""

    def __init__(self, text: str = "What is 25 times 50?", error: Exception | None = None):
        self.text = text
        self.error = error
        self.received: list[bytes] = []

    async def transcribe(self, audio: bytes, *, language: str | None = None) -> Transcript:
        self.received.append(audio)
        if self.error:
            raise self.error
        return Transcript(
            text=self.text, language="en", duration_seconds=2.0, speech_seconds=1.8,
            confidence=0.9, processing_ms=5.0,
        )  # fmt: skip


class FakeVision(VisionProvider):
    """Answers from a script instead of a real vision model; records what it was shown."""

    model = "fake-vision"

    def __init__(self, answer: str = "A calculator showing 84.") -> None:
        self.answer = answer
        self.seen: list[tuple[bytes, str]] = []

    async def describe(self, image: bytes, question: str) -> str:
        self.seen.append((image, question))
        return self.answer


def wav_bytes(seconds: float, *, rate: int = 16000, tone: bool = False) -> bytes:
    """A WAV file: silence, or a 440 Hz tone (not speech) when tone=True."""
    import io
    import math
    import struct
    import wave

    frames = int(seconds * rate)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        if tone:
            wav.writeframes(
                b"".join(
                    struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * i / rate)))
                    for i in range(frames)
                )
            )
        else:
            wav.writeframes(b"\x00\x00" * frames)
    return buffer.getvalue()


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
    documents, retriever = make_documents(db, Path(tempfile.mkdtemp(prefix="arthur-test-")))
    audit = AuditLog(db)
    # Inject fakes instead of running the real lifespan.
    app.state.llm = llm
    app.state.memory = memory_manager
    app.state.documents = documents
    app.state.retriever = retriever
    app.state.stt = FakeSTT()
    app.state.vision = FakeVision()
    app.state.audit = audit
    app.state.tools = create_tool_registry(
        policy=PermissionPolicy(),
        audit=audit,
        http_client=offline_http_client(),
        memory=memory_manager,
        documents=documents,
        retriever=retriever,
        search=WebSearchService(FakeSearchProvider(), retry_delay=0),
        memory_min_score=TEST_SETTINGS.memory_min_score,
        document_min_score=0.2,
    )
    app.state.orchestrator = build_orchestrator(
        llm, TEST_SETTINGS, memory_manager, app.state.tools, retriever
    )
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
