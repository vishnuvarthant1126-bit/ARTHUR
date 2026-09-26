"""Shared test fixtures.

`FakeLLM` implements the same `LLMProvider` interface as Ollama but returns
canned answers instantly - so API tests are fast, free and deterministic.
"""

import asyncio
from collections.abc import AsyncIterator, Iterator

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config.settings import Settings
from app.llm.base import LLMError, LLMProvider, LLMResponse, Message, T
from app.main import build_orchestrator, create_app
from app.observability.logging import configure_logging


class FakeLLM(LLMProvider):
    name = "fake"
    model = "fake-model"

    def __init__(
        self,
        reply: str = "Hello. How can I help?",
        error: LLMError | None = None,
        token_delay: float = 0.0,
    ):
        self.reply = reply
        self.error = error
        self.token_delay = token_delay  # seconds between streamed tokens
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
        return schema.model_validate_json(self.reply)


@pytest.fixture
def fake_llm() -> FakeLLM:
    return FakeLLM()


def _app_with(llm: LLMProvider):
    configure_logging("WARNING")
    app = create_app()
    # Inject the fake instead of running the real lifespan.
    app.state.llm = llm
    app.state.orchestrator = build_orchestrator(llm, Settings(_env_file=None))
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
    replacing the fake LLM with a real Ollama provider.
    """
    yield TestClient(_app_with(fake_llm))
