"""The stand-ins used by the load test (Phase 22): echo model and hash embeddings."""

import time

import pytest

from app.config.settings import Settings
from app.llm.base import LLMResponseError, Message, Role
from app.llm.echo import EchoProvider
from app.llm.factory import build_provider
from app.rag.embeddings import HashEmbeddings, create_embedding_provider

MESSAGES = [Message(role=Role.USER, content="Hello there")]


async def test_echo_answers_without_a_model():
    echo = EchoProvider(words=8)
    response = await echo.generate(MESSAGES)
    assert response.content.startswith("Echo of 11 characters.")
    assert response.completion_tokens == 8
    assert response.tool_calls == []  # it never uses tools

    streamed = [token async for token in echo.stream(MESSAGES)]
    assert "".join(streamed) == response.content
    assert await echo.health()


async def test_echo_takes_the_configured_time():
    echo = EchoProvider(delay_seconds=0.2, words=5)
    start = time.perf_counter()
    await echo.generate(MESSAGES)
    generated = time.perf_counter() - start
    start = time.perf_counter()
    async for _ in echo.stream(MESSAGES):
        pass
    streamed = time.perf_counter() - start
    assert 0.18 <= generated < 0.6
    assert 0.18 <= streamed < 0.8  # spread over the words, like a model writing


async def test_echo_cannot_plan():
    with pytest.raises(LLMResponseError):
        await EchoProvider().generate_structured(MESSAGES, Message)


def test_factory_builds_echo_from_settings():
    settings = Settings(_env_file=None, llm_provider="echo", echo_delay_seconds=1.5, echo_words=12)
    provider = build_provider("echo", settings)
    assert isinstance(provider, EchoProvider)
    assert (provider.delay_seconds, provider.words) == (1.5, 12)


async def test_hash_embeddings_match_shared_words():
    embeddings = create_embedding_provider("hash", "http://unused", "unused")
    assert isinstance(embeddings, HashEmbeddings)

    def similarity(a: list[float], b: list[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b, strict=True))
        norm = (sum(x * x for x in a) ** 0.5) * (sum(y * y for y in b) ** 0.5)
        return dot / norm

    query = await embeddings.embed_query("my cat is called Miso")
    cat, weather = await embeddings.embed_documents(
        ["The cat is called Miso.", "It rains in Singapore."]
    )
    assert len(query) == HashEmbeddings.DIMENSIONS
    assert similarity(query, cat) > 0.6 > similarity(query, weather)
    assert any(await embeddings.embed_query(""))  # an empty text still gives a usable vector

    with pytest.raises(ValueError, match="Supported: ollama, hash"):
        create_embedding_provider("magic", "http://unused", "unused")
