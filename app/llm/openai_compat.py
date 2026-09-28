"""Provider for any OpenAI-compatible Chat Completions API.

Many services speak the same API shape as OpenAI: OpenAI itself, Groq,
OpenRouter, Together, LM Studio, vLLM, and Ollama's own /v1 endpoint.
One class therefore unlocks all of them - only the base URL, key and
model name change (all set in `.env`).
"""

import json
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx
from pydantic import ValidationError

from app.llm._http import status_error, translate_http_errors
from app.llm.base import LLMProvider, LLMResponse, LLMResponseError, Message, Role, T, ToolCall

LABEL = "OpenAI-compatible API"


class OpenAICompatProvider(LLMProvider):
    name = "openai_compat"

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str | None = None,
        timeout_seconds: float = 120.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.model = model
        self._base_url = base_url
        # The key travels only in this header - never in URLs or logs.
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers=headers,
            timeout=httpx.Timeout(timeout_seconds, connect=5.0),
            transport=transport,
        )

    async def generate(
        self,
        messages: list[Message],
        *,
        temperature: float | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        start = time.perf_counter()
        payload = self._payload(messages, stream=False, temperature=temperature)
        if tools:
            payload["tools"] = tools
        data = await self._post(payload)
        usage = data.get("usage") or {}
        return LLMResponse(
            content=self._content(data).strip(),
            model=data.get("model", self.model),
            latency_ms=round((time.perf_counter() - start) * 1000, 1),
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            tool_calls=_parse_tool_calls(data),
        )

    async def stream(
        self, messages: list[Message], *, temperature: float | None = None
    ) -> AsyncIterator[str]:
        payload = self._payload(messages, stream=True, temperature=temperature)
        with self._errors():
            async with self._client.stream("POST", "chat/completions", json=payload) as response:
                if response.status_code >= 400:
                    await response.aread()
                    raise self._status_error(response)
                # Server-Sent Events: lines like `data: {...}`, ending with `data: [DONE]`.
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line.removeprefix("data:").strip()
                    if data == "[DONE]":
                        break
                    chunk = json.loads(data)
                    if error := chunk.get("error"):
                        raise LLMResponseError(str(error))
                    choices = chunk.get("choices") or [{}]
                    if token := (choices[0].get("delta") or {}).get("content"):
                        yield token

    async def generate_structured(self, messages: list[Message], schema: type[T]) -> T:
        payload = self._payload(messages, stream=False, temperature=0.0)
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": schema.__name__, "schema": schema.model_json_schema()},
        }
        content = self._content(await self._post(payload))
        try:
            return schema.model_validate_json(content)
        except ValidationError as exc:
            raise LLMResponseError(
                f"Model output did not match {schema.__name__}: {exc.errors()[:3]}"
            ) from exc

    async def health(self) -> bool:
        try:
            response = await self._client.get("models", timeout=5.0)
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
            "messages": [_to_openai(m) for m in messages],
            "stream": stream,
        }
        if temperature is not None:
            payload["temperature"] = temperature
        return payload

    async def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._errors():
            response = await self._client.post("chat/completions", json=payload)
        if response.status_code >= 400:
            raise self._status_error(response)
        return response.json()

    @staticmethod
    def _content(data: dict[str, Any]) -> str:
        try:
            return data["choices"][0]["message"].get("content") or ""
        except (KeyError, IndexError, TypeError, AttributeError) as exc:
            raise LLMResponseError(f"Unexpected response shape from {LABEL}.") from exc

    def _errors(self):
        return translate_http_errors(LABEL, self._base_url)

    def _status_error(self, response: httpx.Response) -> LLMResponseError:
        if response.status_code in (401, 403):
            return LLMResponseError(
                f"{LABEL} rejected the API key (HTTP {response.status_code}). "
                "Check OPENAI_COMPAT_API_KEY in .env."
            )
        if response.status_code == 404:
            return LLMResponseError(
                f"{LABEL}: model '{self.model}' or endpoint not found. "
                "Check OPENAI_COMPAT_BASE_URL and OPENAI_COMPAT_MODEL."
            )
        return status_error(LABEL, response)


def _to_openai(message: Message) -> dict[str, Any]:
    data: dict[str, Any] = {"role": message.role.value, "content": message.content}
    if message.tool_calls:
        data["content"] = message.content or None
        data["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                # OpenAI sends and expects arguments as a JSON *string*.
                "function": {"name": call.name, "arguments": json.dumps(call.arguments)},
            }
            for call in message.tool_calls
        ]
    if message.role == Role.TOOL:
        data["tool_call_id"] = message.tool_call_id
    return data


def _parse_tool_calls(data: dict[str, Any]) -> list[ToolCall]:
    try:
        raw = data["choices"][0]["message"].get("tool_calls") or []
    except (KeyError, IndexError, TypeError, AttributeError):
        return []
    calls = []
    for item in raw:
        function = item.get("function", {})
        try:
            arguments = json.loads(function.get("arguments") or "{}")
        except ValueError:
            arguments = {}  # invalid JSON -> the registry reports the missing arguments
        calls.append(
            ToolCall(
                id=item.get("id", ""),
                name=function.get("name", ""),
                arguments=arguments if isinstance(arguments, dict) else {},
            )
        )
    return calls
