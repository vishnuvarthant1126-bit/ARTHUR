"""Shared test fixtures.

`FakeLLM` implements the same `LLMProvider` interface as Ollama but returns
canned answers instantly - so API tests are fast, free and deterministic.
"""

from collections.abc import AsyncIterator

import httpx
import pytest

from app.llm.base import LLMError, LLMProvider, LLMResponse, Message, T
from app.main import create_app
from app.observability.logging import configure_logging


class FakeLLM(LLMProvider):
    name = "fake"
    model = "fake-model"

    def __init__(self, reply: str = "Hello. How can I help?", error: LLMError | None = None):
        self.reply = reply
        self.error = error
        self.calls: list[list[Message]] = []

    async def generate(self, messages, *, temperature=None) -> LLMResponse:
        self.calls.append(messages)
        if self.error:
            raise self.error
        return LLMResponse(content=self.reply, model=self.model, latency_ms=1.0)

    async def stream(self, messages, *, temperature=None) -> AsyncIterator[str]:
        for word in self.reply.split(" "):
            yield word + " "

    async def generate_structured(self, messages, schema: type[T]) -> T:
        return schema.model_validate_json(self.reply)


@pytest.fixture
def fake_llm() -> FakeLLM:
    return FakeLLM()


@pytest.fixture
async def client(fake_llm: FakeLLM) -> AsyncIterator[httpx.AsyncClient]:
    configure_logging("WARNING")
    app = create_app()
    app.state.llm = fake_llm  # inject the fake instead of running the real lifespan
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
