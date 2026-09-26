"""Tests for OllamaProvider without a real Ollama.

httpx.MockTransport lets us intercept HTTP requests and return any response
we want, so we can check what ARTHUR sends and how it handles each reply.
"""

import json

import httpx
import pytest
from pydantic import BaseModel

from app.llm.base import (
    LLMResponseError,
    LLMTimeoutError,
    LLMUnavailableError,
    Message,
    Role,
)
from app.llm.ollama import OllamaProvider

MESSAGES = [
    Message(role=Role.SYSTEM, content="You are ARTHUR."),
    Message(role=Role.USER, content="Hi"),
]


def make_provider(handler) -> OllamaProvider:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://ollama.test"
    )
    return OllamaProvider(base_url="http://ollama.test", model="qwen3:8b", client=client)


async def test_generate_sends_messages_and_parses_reply():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "model": "qwen3:8b",
                "message": {"role": "assistant", "content": "  Hello!  "},
                "prompt_eval_count": 20,
                "eval_count": 3,
            },
        )

    result = await make_provider(handler).generate(MESSAGES, temperature=0.2)

    assert captured["path"] == "/api/chat"
    assert captured["body"]["model"] == "qwen3:8b"
    assert captured["body"]["stream"] is False
    assert captured["body"]["think"] is False
    assert captured["body"]["options"] == {"temperature": 0.2}
    assert captured["body"]["messages"] == [
        {"role": "system", "content": "You are ARTHUR."},
        {"role": "user", "content": "Hi"},
    ]
    assert result.content == "Hello!"
    assert result.prompt_tokens == 20
    assert result.completion_tokens == 3


async def test_missing_model_gives_pull_hint():
    provider = make_provider(lambda r: httpx.Response(404, json={"error": "model not found"}))
    with pytest.raises(LLMResponseError, match="ollama pull qwen3:8b"):
        await provider.generate(MESSAGES)


async def test_server_error_is_llm_response_error():
    provider = make_provider(lambda r: httpx.Response(500, json={"error": "out of memory"}))
    with pytest.raises(LLMResponseError, match="out of memory"):
        await provider.generate(MESSAGES)


async def test_connection_refused_is_unavailable():
    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(LLMUnavailableError, match="Cannot reach Ollama"):
        await make_provider(handler).generate(MESSAGES)


async def test_timeout_is_llm_timeout():
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(LLMTimeoutError):
        await make_provider(handler).generate(MESSAGES)


async def test_stream_yields_tokens_in_order():
    lines = [
        {"message": {"content": "Hel"}, "done": False},
        {"message": {"content": "lo"}, "done": False},
        {"message": {"content": ""}, "done": True},
    ]
    body = "\n".join(json.dumps(line) for line in lines)
    provider = make_provider(lambda r: httpx.Response(200, text=body))

    tokens = [token async for token in provider.stream(MESSAGES)]

    assert tokens == ["Hel", "lo"]


async def test_stream_connection_error_is_unavailable():
    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(LLMUnavailableError):
        [t async for t in make_provider(handler).stream(MESSAGES)]


class Weather(BaseModel):
    city: str
    celsius: float


async def test_generate_structured_validates_output():
    captured = {}

    def handler(request):
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200, json={"message": {"content": '{"city": "Singapore", "celsius": 31.5}'}}
        )

    result = await make_provider(handler).generate_structured(MESSAGES, Weather)

    assert result == Weather(city="Singapore", celsius=31.5)
    assert captured["body"]["format"]["properties"].keys() == {"city", "celsius"}


async def test_generate_structured_rejects_bad_output():
    provider = make_provider(
        lambda r: httpx.Response(200, json={"message": {"content": '{"city": "Singapore"}'}})
    )
    with pytest.raises(LLMResponseError, match="did not match Weather"):
        await provider.generate_structured(MESSAGES, Weather)


async def test_health_false_when_unreachable():
    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    assert await make_provider(handler).health() is False
