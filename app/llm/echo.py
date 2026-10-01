"""A stand-in "model" for load tests and offline demos (LLM_PROVIDER=echo).

It needs no GPU and no Ollama: it waits a configurable time (like a model thinking)
and answers with a fixed number of words. A load test must measure ARTHUR's own code -
with the real model in the loop, every result would just be "the GPU is the bottleneck".

Never use it for real work: it understands nothing and never calls tools.
"""

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Any

from app.llm.base import LLMProvider, LLMResponse, LLMResponseError, Message, T


class EchoProvider(LLMProvider):
    name = "echo"

    def __init__(self, delay_seconds: float = 0.0, words: int = 30) -> None:
        self.model = "echo"
        self.delay_seconds = delay_seconds  # total "thinking + writing" time per answer
        self.words = max(words, 1)

    def _answer(self, messages: list[Message]) -> list[str]:
        last = next((m.content for m in reversed(messages) if m.role.value == "user"), "")
        text = f"Echo of {len(last)} characters. " + "word " * self.words
        return text.split(" ")[: self.words]

    async def generate(
        self,
        messages: list[Message],
        *,
        temperature: float | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        start = time.perf_counter()
        await asyncio.sleep(self.delay_seconds)
        words = self._answer(messages)
        return LLMResponse(
            content=" ".join(words),
            model=self.model,
            latency_ms=round((time.perf_counter() - start) * 1000, 1),
            prompt_tokens=sum(len(m.content) for m in messages) // 4,
            completion_tokens=len(words),
        )

    async def stream(
        self, messages: list[Message], *, temperature: float | None = None
    ) -> AsyncIterator[str]:
        words = self._answer(messages)
        pause = self.delay_seconds / len(words)
        for index, word in enumerate(words):
            await asyncio.sleep(pause)
            yield word if index == 0 else " " + word

    async def generate_structured(self, messages: list[Message], schema: type[T]) -> T:
        raise LLMResponseError("The echo provider can't produce structured output.")
