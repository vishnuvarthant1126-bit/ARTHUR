"""Tests for retries, fallback and the provider factory."""

import pytest
from pydantic import SecretStr

from app.config.settings import Settings
from app.llm.base import LLMResponseError, LLMUnavailableError, Message, Role
from app.llm.factory import create_llm_provider
from app.llm.ollama import OllamaProvider
from app.llm.openai_compat import OpenAICompatProvider
from app.llm.resilience import FallbackProvider, RetryingProvider
from tests.conftest import FakeLLM

MESSAGES = [Message(role=Role.USER, content="Hi")]


class FlakyLLM(FakeLLM):
    """Fails the first `failures` calls with `error`, then works."""

    def __init__(self, failures: int, error: Exception, **kwargs):
        super().__init__(**kwargs)
        self.failures = failures
        self.failure_error = error
        self.attempts = 0

    async def generate(self, messages, *, temperature=None, tools=None):
        self.attempts += 1
        if self.attempts <= self.failures:
            raise self.failure_error
        return await super().generate(messages)

    async def stream(self, messages, *, temperature=None):
        self.attempts += 1
        if self.attempts <= self.failures:
            raise self.failure_error
        async for token in super().stream(messages):
            yield token


def retrying(inner, retries=2):
    return RetryingProvider(inner, max_retries=retries, base_delay=0)  # no real waiting


async def test_retry_recovers_from_brief_outage():
    flaky = FlakyLLM(failures=2, error=LLMUnavailableError("down"))

    result = await retrying(flaky).generate(MESSAGES)

    assert result.content == "Hello. How can I help?"
    assert flaky.attempts == 3


async def test_retry_gives_up_after_max_retries():
    flaky = FlakyLLM(failures=5, error=LLMUnavailableError("down"))

    with pytest.raises(LLMUnavailableError):
        await retrying(flaky, retries=2).generate(MESSAGES)
    assert flaky.attempts == 3


async def test_non_retryable_error_fails_immediately():
    flaky = FlakyLLM(failures=1, error=LLMResponseError("bad key", retryable=False))

    with pytest.raises(LLMResponseError):
        await retrying(flaky).generate(MESSAGES)
    assert flaky.attempts == 1


async def test_stream_retries_before_first_token():
    flaky = FlakyLLM(failures=1, error=LLMUnavailableError("down"))

    tokens = [t async for t in retrying(flaky).stream(MESSAGES)]

    assert "".join(tokens) == "Hello. How can I help?"


class BreaksMidStream(FakeLLM):
    async def stream(self, messages, *, temperature=None):
        yield "Hel"
        raise LLMUnavailableError("connection dropped")


async def test_stream_does_not_retry_after_tokens_were_sent():
    received = []
    with pytest.raises(LLMUnavailableError):
        async for token in retrying(BreaksMidStream()).stream(MESSAGES):
            received.append(token)
    assert received == ["Hel"]  # no duplicated "Hel" from a silent restart


async def test_fallback_uses_backup_when_primary_is_down():
    primary = FakeLLM(error=LLMUnavailableError("ollama down"))
    backup = FakeLLM(reply="from backup")

    result = await FallbackProvider([primary, backup]).generate(MESSAGES)

    assert result.content == "from backup"


async def test_fallback_stream_uses_backup():
    primary = FakeLLM(error=LLMUnavailableError("ollama down"))
    backup = FakeLLM(reply="from backup")

    tokens = [t async for t in FallbackProvider([primary, backup]).stream(MESSAGES)]

    assert "".join(tokens) == "from backup"


async def test_fallback_raises_last_error_when_all_fail():
    providers = [FakeLLM(error=LLMUnavailableError(f"down {i}")) for i in range(2)]

    with pytest.raises(LLMUnavailableError, match="down 1"):
        await FallbackProvider(providers).generate(MESSAGES)


def settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


def test_factory_default_is_retrying_ollama():
    provider = create_llm_provider(settings())

    assert isinstance(provider, RetryingProvider)
    assert isinstance(provider.inner, OllamaProvider)
    assert provider.model == "qwen3:8b"


def test_factory_builds_fallback_chain():
    provider = create_llm_provider(
        settings(
            llm_fallback_provider="openai_compat",
            openai_compat_base_url="https://api.example.com/v1",
            openai_compat_model="gpt-test",
            openai_compat_api_key=SecretStr("sk-test"),
        )
    )

    assert isinstance(provider, FallbackProvider)
    assert isinstance(provider.providers[0].inner, OllamaProvider)
    assert isinstance(provider.providers[1].inner, OpenAICompatProvider)


def test_factory_rejects_incomplete_openai_config():
    with pytest.raises(ValueError, match="OPENAI_COMPAT_BASE_URL"):
        create_llm_provider(settings(llm_provider="openai_compat"))


def test_factory_rejects_unknown_provider():
    with pytest.raises(ValueError, match="Unknown LLM provider 'skynet'"):
        create_llm_provider(settings(llm_provider="skynet"))


def test_blank_env_values_mean_not_set():
    s = settings(llm_fallback_provider="", openai_compat_api_key="")
    assert s.llm_fallback_provider is None
    assert s.openai_compat_api_key is None
