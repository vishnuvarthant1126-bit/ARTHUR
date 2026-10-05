"""Retry and fallback wrappers around any LLMProvider.

Both use the *decorator pattern*: they implement the same LLMProvider
interface and wrap another provider. The rest of ARTHUR can't tell the
difference - it just gets a more reliable provider.

    FallbackProvider([ RetryingProvider(Ollama), RetryingProvider(OpenAICompat) ])
"""

import asyncio
import random
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, TypeVar

from app.llm.base import (
    LLMError,
    LLMProvider,
    LLMResponse,
    LLMUnavailableError,
    Message,
    StreamEvent,
    T,
)
from app.observability.logging import get_logger

log = get_logger(__name__)
R = TypeVar("R")
S = TypeVar("S")  # an item in a stream: a text token or a StreamEvent


class RetryingProvider(LLMProvider):
    """Retry brief failures with exponential backoff (0.5s, 1s, 2s ... + random jitter).

    Jitter (a small random extra wait) stops many clients retrying in lockstep.
    Only errors marked `retryable` are retried; a bad API key fails immediately.

    Circuit breaker (Phase 28): once the provider was unreachable even after the retries,
    the next `down_seconds` get ONE attempt, no retries. Measured on Windows: a refused
    local connection takes 2 s, so with retries "Ollama is off" took 10 s to report -
    every time. Now that happens once; the next messages fail fast until it is back.
    """

    def __init__(
        self,
        inner: LLMProvider,
        max_retries: int = 2,
        base_delay: float = 0.5,
        down_seconds: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.inner = inner
        self.name = inner.name
        self.model = inner.model
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.down_seconds = down_seconds
        self.clock = clock
        self._down_since: float | None = None  # when the provider was last found unreachable

    @property
    def known_down(self) -> bool:
        return self._down_since is not None and self.clock() - self._down_since < self.down_seconds

    async def generate(
        self,
        messages: list[Message],
        *,
        temperature: float | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        return await self._with_retries(
            lambda: self.inner.generate(messages, temperature=temperature, tools=tools)
        )

    async def generate_structured(self, messages: list[Message], schema: type[T]) -> T:
        return await self._with_retries(lambda: self.inner.generate_structured(messages, schema))

    def stream(
        self, messages: list[Message], *, temperature: float | None = None
    ) -> AsyncIterator[str]:
        return self._retry_stream(lambda: self.inner.stream(messages, temperature=temperature))

    def stream_chat(
        self,
        messages: list[Message],
        *,
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
    ) -> AsyncIterator[StreamEvent]:
        return self._retry_stream(
            lambda: self.inner.stream_chat(messages, tools=tools, temperature=temperature)
        )

    async def _retry_stream(self, open_stream: Callable[[], AsyncIterator[S]]) -> AsyncIterator[S]:
        for attempt in range(self.max_retries + 1):
            started = False
            try:
                async for item in open_stream():
                    started = True
                    yield item
                self._down_since = None
                return
            except LLMError as exc:
                # Once words reached the user we can't silently restart the answer.
                if started or not self._should_retry(exc, attempt):
                    self._note_failure(exc)
                    raise
                await self._wait(exc, attempt)

    async def health(self) -> bool:
        return await self.inner.health()

    async def aclose(self) -> None:
        await self.inner.aclose()

    async def _with_retries(self, call: Callable[[], Awaitable[R]]) -> R:
        for attempt in range(self.max_retries + 1):
            try:
                result = await call()
                self._down_since = None
                return result
            except LLMError as exc:
                if not self._should_retry(exc, attempt):
                    self._note_failure(exc)
                    raise
                await self._wait(exc, attempt)
        raise AssertionError("unreachable")

    def _should_retry(self, exc: LLMError, attempt: int) -> bool:
        if isinstance(exc, LLMUnavailableError) and self.known_down:
            return False  # it was down a moment ago: report it now instead of waiting again
        return exc.retryable and attempt < self.max_retries

    def _note_failure(self, exc: LLMError) -> None:
        if isinstance(exc, LLMUnavailableError) and not self.known_down:
            self._down_since = self.clock()

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
        self,
        messages: list[Message],
        *,
        temperature: float | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        return await self._first_success(
            lambda p: p.generate(messages, temperature=temperature, tools=tools)
        )

    async def generate_structured(self, messages: list[Message], schema: type[T]) -> T:
        return await self._first_success(lambda p: p.generate_structured(messages, schema))

    def stream(
        self, messages: list[Message], *, temperature: float | None = None
    ) -> AsyncIterator[str]:
        return self._fallback_stream(lambda p: p.stream(messages, temperature=temperature))

    def stream_chat(
        self,
        messages: list[Message],
        *,
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
    ) -> AsyncIterator[StreamEvent]:
        return self._fallback_stream(
            lambda p: p.stream_chat(messages, tools=tools, temperature=temperature)
        )

    async def _fallback_stream(
        self, open_stream: Callable[[LLMProvider], AsyncIterator[S]]
    ) -> AsyncIterator[S]:
        for index, provider in enumerate(self.providers):
            started = False
            try:
                async for item in open_stream(provider):
                    started = True
                    yield item
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
