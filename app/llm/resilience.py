"""Retry and fallback wrappers around any LLMProvider.

Both use the *decorator pattern*: they implement the same LLMProvider
interface and wrap another provider. The rest of ARTHUR can't tell the
difference - it just gets a more reliable provider.

    FallbackProvider([ RetryingProvider(Ollama), RetryingProvider(OpenAICompat) ])
"""

import asyncio
import random
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import TypeVar

from app.llm.base import LLMError, LLMProvider, LLMResponse, Message, T
from app.observability.logging import get_logger

log = get_logger(__name__)
R = TypeVar("R")


class RetryingProvider(LLMProvider):
    """Retry brief failures with exponential backoff (0.5s, 1s, 2s ... + random jitter).

    Jitter (a small random extra wait) stops many clients retrying in lockstep.
    Only errors marked `retryable` are retried; a bad API key fails immediately.
    """

    def __init__(self, inner: LLMProvider, max_retries: int = 2, base_delay: float = 0.5):
        self.inner = inner
        self.name = inner.name
        self.model = inner.model
        self.max_retries = max_retries
        self.base_delay = base_delay

    async def generate(
        self, messages: list[Message], *, temperature: float | None = None
    ) -> LLMResponse:
        return await self._with_retries(
            lambda: self.inner.generate(messages, temperature=temperature)
        )

    async def generate_structured(self, messages: list[Message], schema: type[T]) -> T:
        return await self._with_retries(lambda: self.inner.generate_structured(messages, schema))

    async def stream(
        self, messages: list[Message], *, temperature: float | None = None
    ) -> AsyncIterator[str]:
        for attempt in range(self.max_retries + 1):
            started = False
            try:
                async for token in self.inner.stream(messages, temperature=temperature):
                    started = True
                    yield token
                return
            except LLMError as exc:
                # Once words reached the user we can't silently restart the answer.
                if started or not self._should_retry(exc, attempt):
                    raise
                await self._wait(exc, attempt)

    async def health(self) -> bool:
        return await self.inner.health()

    async def aclose(self) -> None:
        await self.inner.aclose()

    async def _with_retries(self, call: Callable[[], Awaitable[R]]) -> R:
        for attempt in range(self.max_retries + 1):
            try:
                return await call()
            except LLMError as exc:
                if not self._should_retry(exc, attempt):
                    raise
                await self._wait(exc, attempt)
        raise AssertionError("unreachable")

    def _should_retry(self, exc: LLMError, attempt: int) -> bool:
        return exc.retryable and attempt < self.max_retries

    async def _wait(self, exc: LLMError, attempt: int) -> None:
        delay = self.base_delay * 2**attempt + random.uniform(0, self.base_delay)
        log.warning(
            "llm_retry",
            provider=self.name,
            attempt=attempt + 1,
            delay_s=round(delay, 2),
            error=str(exc),
        )
        await asyncio.sleep(delay)


class FallbackProvider(LLMProvider):
    """Try providers in order; if one fails, use the next (e.g. local -> cloud)."""

    def __init__(self, providers: list[LLMProvider]):
        if not providers:
            raise ValueError("FallbackProvider needs at least one provider")
        self.providers = providers
        self.name = providers[0].name
        self.model = providers[0].model

    async def generate(
        self, messages: list[Message], *, temperature: float | None = None
    ) -> LLMResponse:
        return await self._first_success(lambda p: p.generate(messages, temperature=temperature))

    async def generate_structured(self, messages: list[Message], schema: type[T]) -> T:
        return await self._first_success(lambda p: p.generate_structured(messages, schema))

    async def stream(
        self, messages: list[Message], *, temperature: float | None = None
    ) -> AsyncIterator[str]:
        for index, provider in enumerate(self.providers):
            started = False
            try:
                async for token in provider.stream(messages, temperature=temperature):
                    started = True
                    yield token
                return
            except LLMError as exc:
                if started or index == len(self.providers) - 1:
                    raise
                self._log_fallback(provider, exc)

    async def health(self) -> bool:
        results = await asyncio.gather(*(p.health() for p in self.providers))
        return any(results)

    async def aclose(self) -> None:
        for provider in self.providers:
            await provider.aclose()

    async def _first_success(self, call: Callable[[LLMProvider], Awaitable[R]]) -> R:
        for index, provider in enumerate(self.providers):
            try:
                return await call(provider)
            except LLMError as exc:
                if index == len(self.providers) - 1:
                    raise
                self._log_fallback(provider, exc)
        raise AssertionError("unreachable")

    @staticmethod
    def _log_fallback(provider: LLMProvider, exc: LLMError) -> None:
        log.warning("llm_fallback", failed_provider=provider.name, error=str(exc))
