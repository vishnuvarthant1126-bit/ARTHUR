"""Measure every call to the language model (decorator pattern, like RetryingProvider).

    MeteredProvider( RetryingProvider( OllamaProvider ) )

Records per call: how long it took, whether it failed, tokens used and - for streamed
answers - the "time to first token" (how long until the user sees something).
"""

import time
from collections.abc import AsyncIterator
from typing import Any

from app.llm.base import (
    LLMError,
    LLMProvider,
    LLMResponse,
    Message,
    StreamEvent,
    StreamStats,
    T,
    TextDelta,
)
from app.observability.metrics import Metrics


class MeteredProvider(LLMProvider):
    def __init__(self, inner: LLMProvider, metrics: Metrics) -> None:
        self.inner = inner
        self.name = inner.name
        self.model = inner.model
        self.metrics = metrics

    async def generate(
        self,
        messages: list[Message],
        *,
        temperature: float | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        start = time.perf_counter()
        try:
            response = await self.inner.generate(messages, temperature=temperature, tools=tools)
        except LLMError:
            self._done("generate", "error", start)
            raise
        self._done("generate", "ok", start)
        self._tokens("prompt", response.prompt_tokens)
        self._tokens("completion", response.completion_tokens)
        return response

    async def generate_structured(self, messages: list[Message], schema: type[T]) -> T:
        start = time.perf_counter()
        try:
            result = await self.inner.generate_structured(messages, schema)
        except LLMError:
            self._done("structured", "error", start)
            raise
        self._done("structured", "ok", start)
        return result

    # Not `async def`: these return the measuring generator itself. With one more
    # generator wrapped around it, pressing Stop would close only the outer one and
    # the "stopped" outcome would be recorded late (found by a test).
    def stream(
        self, messages: list[Message], *, temperature: float | None = None
    ) -> AsyncIterator[str]:
        return self._metered(self.inner.stream(messages, temperature=temperature))

    def stream_chat(
        self,
        messages: list[Message],
        *,
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
    ) -> AsyncIterator[StreamEvent]:
        return self._metered(self.inner.stream_chat(messages, tools=tools, temperature=temperature))

    async def health(self) -> bool:
        return await self.inner.health()

    async def aclose(self) -> None:
        await self.inner.aclose()

    # ---------- helpers ----------

    async def _metered(self, stream: AsyncIterator) -> AsyncIterator:
        start = time.perf_counter()
        first = True
        chunks = 0
        exact = False  # did the provider report exact token counts?
        outcome = "stopped"  # the user pressed Stop, or the tab closed, before the end
        try:
            async for event in stream:
                if isinstance(event, StreamStats):  # the provider's own measurements:
                    self._record(event)  # recorded here and NOT passed on
                    exact = True
                    continue
                if first:
                    first = False
                    self.metrics.llm_first_token.labels(self.model).observe(
                        time.perf_counter() - start
                    )
                if isinstance(event, (str, TextDelta)):
                    chunks += 1
                yield event
            outcome = "ok"
        except LLMError:
            outcome = "error"
            raise
        finally:
            if not exact:
                self._tokens("completion", chunks)  # one chunk is roughly one token
            self._done("stream", outcome, start)

    def _record(self, stats: StreamStats) -> None:
        self._tokens("prompt", stats.prompt_tokens)
        self._tokens("completion", stats.completion_tokens)
        if stats.prompt_tokens:
            self.metrics.llm_prompt_tokens.labels(self.model).observe(stats.prompt_tokens)
        self.metrics.llm_prompt_read.labels(self.model).observe(stats.prompt_seconds)
        self.metrics.llm_load.labels(self.model).observe(stats.load_seconds)
        if stats.generation_seconds > 0:
            self.metrics.llm_speed.labels(self.model).set(
                stats.completion_tokens / stats.generation_seconds
            )

    def _done(self, kind: str, outcome: str, start: float) -> None:
        self.metrics.llm_requests.labels(self.model, kind, outcome).inc()
        self.metrics.llm_duration.labels(self.model).observe(time.perf_counter() - start)

    def _tokens(self, kind: str, count: int | None) -> None:
        if count:
            self.metrics.llm_tokens.labels(self.model, kind).inc(count)
