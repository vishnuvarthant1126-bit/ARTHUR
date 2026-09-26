"""Ollama implementation of `LLMProvider`.

Ollama runs models locally and exposes an HTTP API on port 11434.
We use its `/api/chat` endpoint, which takes a list of role/content
messages - the same shape as our `Message` model.
"""

import json
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from typing import Any

import httpx
from pydantic import ValidationError

from app.llm.base import (
    LLMProvider,
    LLMResponse,
    LLMResponseError,
    LLMTimeoutError,
    LLMUnavailableError,
    Message,
    T,
)


class OllamaProvider(LLMProvider):
    name = "ollama"

    def __init__(
        self,
        base_url: str,
        model: str,
        timeout_seconds: float = 120.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.model = model
        self._base_url = base_url
        # One shared client = connection pooling (reuses TCP connections).
        # connect timeout is short: if Ollama isn't running we want to know fast.
        self._client = client or httpx.AsyncClient(
            base_url=base_url, timeout=httpx.Timeout(timeout_seconds, connect=5.0)
        )

    # ---------- public API ----------

    async def generate(
        self, messages: list[Message], *, temperature: float | None = None
    ) -> LLMResponse:
        payload = self._payload(messages, stream=False, temperature=temperature)
        start = time.perf_counter()
        data = await self._post_chat(payload)
        return LLMResponse(
            content=data.get("message", {}).get("content", "").strip(),
            model=data.get("model", self.model),
            latency_ms=round((time.perf_counter() - start) * 1000, 1),
            prompt_tokens=data.get("prompt_eval_count"),
            completion_tokens=data.get("eval_count"),
        )

    async def stream(
        self, messages: list[Message], *, temperature: float | None = None
    ) -> AsyncIterator[str]:
        payload = self._payload(messages, stream=True, temperature=temperature)
        with self._translate_errors():
            async with self._client.stream("POST", "/api/chat", json=payload) as response:
                if response.status_code >= 400:
                    await response.aread()
                    raise self._status_error(response)
                # Ollama streams one JSON object per line ("NDJSON").
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    chunk = json.loads(line)
                    if error := chunk.get("error"):
                        raise LLMResponseError(error)
                    if token := chunk.get("message", {}).get("content"):
                        yield token
                    if chunk.get("done"):
                        break

    async def generate_structured(self, messages: list[Message], schema: type[T]) -> T:
        # Ollama's `format` accepts a JSON Schema and constrains the output to it.
        payload = self._payload(messages, stream=False, temperature=0.0)
        payload["format"] = schema.model_json_schema()
        data = await self._post_chat(payload)
        content = data.get("message", {}).get("content", "")
        try:
            return schema.model_validate_json(content)
        except ValidationError as exc:
            raise LLMResponseError(
                f"Model output did not match {schema.__name__}: {exc.errors()[:3]}"
            ) from exc

    async def health(self) -> bool:
        try:
            response = await self._client.get("/api/tags", timeout=3.0)
            return response.status_code == 200
        except httpx.HTTPError:
            return False

    async def aclose(self) -> None:
        await self._client.aclose()

    # ---------- internals ----------

    def _payload(
        self, messages: list[Message], *, stream: bool, temperature: float | None
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [m.model_dump(mode="json") for m in messages],
            "stream": stream,
            # qwen3 is a "thinking" model; hidden reasoning adds latency and
            # would leak <think> text into replies. Planning (Phase 8) can re-enable it.
            "think": False,
        }
        if temperature is not None:
            payload["options"] = {"temperature": temperature}
        return payload

    async def _post_chat(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._translate_errors():
            response = await self._client.post("/api/chat", json=payload)
        if response.status_code >= 400:
            raise self._status_error(response)
        return response.json()

    @contextmanager
    def _translate_errors(self) -> Iterator[None]:
        """Turn low-level httpx errors into ARTHUR's provider-neutral LLM errors."""
        try:
            yield
        except httpx.ConnectError as exc:
            raise LLMUnavailableError(
                f"Cannot reach Ollama at {self._base_url}. Is Ollama running?"
            ) from exc
        except httpx.TimeoutException as exc:
            raise LLMTimeoutError(f"Ollama did not respond in time ({exc!r}).") from exc

    def _status_error(self, response: httpx.Response) -> LLMResponseError:
        try:
            detail = response.json().get("error", response.text)
        except ValueError:
            detail = response.text
        if response.status_code == 404:
            return LLMResponseError(
                f"Model '{self.model}' is not installed. Run: ollama pull {self.model}"
            )
        return LLMResponseError(f"Ollama returned HTTP {response.status_code}: {detail[:300]}")
