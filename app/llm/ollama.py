"""Ollama implementation of `LLMProvider`.

Ollama runs models locally and exposes an HTTP API on port 11434.
We use its `/api/chat` endpoint, which takes a list of role/content
messages - the same shape as our `Message` model - plus an optional list
of tools the model may ask for.
"""

import json
import time
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import httpx
from pydantic import ValidationError

from app.llm._http import status_error, translate_http_errors
from app.llm.base import (
    LLMProvider,
    LLMResponse,
    LLMResponseError,
    Message,
    Role,
    StreamEvent,
    T,
    TextDelta,
    ToolCall,
    ToolCallsRequested,
)


class OllamaProvider(LLMProvider):
    name = "ollama"

    def __init__(
        self,
        base_url: str,
        model: str,
        timeout_seconds: float = 120.0,
        context_tokens: int | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.model = model
        self._base_url = base_url
        # Ollama's default context window is small; conversation memory needs more.
        self._context_tokens = context_tokens
        # One shared client = connection pooling (reuses TCP connections).
        # connect timeout is short: if Ollama isn't running we want to know fast.
        self._client = client or httpx.AsyncClient(
            base_url=base_url, timeout=httpx.Timeout(timeout_seconds, connect=5.0)
        )

    # ---------- public API ----------

    async def generate(
        self,
        messages: list[Message],
        *,
        temperature: float | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        payload = self._payload(messages, stream=False, temperature=temperature, tools=tools)
        start = time.perf_counter()
        data = await self._post_chat(payload)
        message = data.get("message", {})
        return LLMResponse(
            content=message.get("content", "").strip(),
            model=data.get("model", self.model),
            latency_ms=round((time.perf_counter() - start) * 1000, 1),
            prompt_tokens=data.get("prompt_eval_count"),
            completion_tokens=data.get("eval_count"),
            tool_calls=_parse_tool_calls(message.get("tool_calls")),
        )

    async def stream(
        self, messages: list[Message], *, temperature: float | None = None
    ) -> AsyncIterator[str]:
        async for event in self.stream_chat(messages, temperature=temperature):
            if isinstance(event, TextDelta):
                yield event.text

    async def stream_chat(
        self,
        messages: list[Message],
        *,
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
    ) -> AsyncIterator[StreamEvent]:
        payload = self._payload(messages, stream=True, temperature=temperature, tools=tools)
        calls: list[ToolCall] = []
        with self._errors():
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
                    message = chunk.get("message", {})
                    if token := message.get("content"):
                        yield TextDelta(text=token)
                    calls.extend(_parse_tool_calls(message.get("tool_calls")))
                    if chunk.get("done"):
                        break
        if calls:
            yield ToolCallsRequested(calls=calls)

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
        self,
        messages: list[Message],
        *,
        stream: bool,
        temperature: float | None,
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [_to_ollama(m) for m in messages],
            "stream": stream,
            # qwen3 is a "thinking" model; hidden reasoning adds latency and
            # would leak <think> text into replies.
            "think": False,
        }
        if tools:
            payload["tools"] = tools
        options: dict[str, Any] = {}
        if temperature is not None:
            options["temperature"] = temperature
        if self._context_tokens:
            options["num_ctx"] = self._context_tokens
        if options:
            payload["options"] = options
        return payload

    async def _post_chat(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._errors():
            response = await self._client.post("/api/chat", json=payload)
        if response.status_code >= 400:
            raise self._status_error(response)
        return response.json()

    def _errors(self):
        return translate_http_errors("Ollama", self._base_url)

    def _status_error(self, response: httpx.Response) -> LLMResponseError:
        if response.status_code == 404:
            return LLMResponseError(
                f"Model '{self.model}' is not installed. Run: ollama pull {self.model}"
            )
        return status_error("Ollama", response)


def _to_ollama(message: Message) -> dict[str, Any]:
    data: dict[str, Any] = {"role": message.role.value, "content": message.content}
    if message.tool_calls:
        data["tool_calls"] = [
            {"function": {"name": c.name, "arguments": c.arguments}} for c in message.tool_calls
        ]
    if message.role == Role.TOOL and message.name:
        data["tool_name"] = message.name
    return data


def _parse_tool_calls(raw: list[dict] | None) -> list[ToolCall]:
    calls = []
    for item in raw or []:
        function = item.get("function", {})
        arguments = function.get("arguments") or {}
        if isinstance(arguments, str):  # some models return a JSON string
            try:
                arguments = json.loads(arguments)
            except ValueError:
                arguments = {}
        calls.append(
            ToolCall(
                id=item.get("id") or f"call_{uuid4().hex[:8]}",
                name=function.get("name", ""),
                arguments=arguments if isinstance(arguments, dict) else {},
            )
        )
    return calls
