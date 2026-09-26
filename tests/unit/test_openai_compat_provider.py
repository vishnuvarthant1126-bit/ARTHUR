"""Tests for OpenAICompatProvider with a mocked HTTP server."""

import json

import httpx
import pytest
from pydantic import BaseModel

from app.llm.base import LLMResponseError, LLMUnavailableError, Message, Role
from app.llm.openai_compat import OpenAICompatProvider

MESSAGES = [Message(role=Role.USER, content="Hi")]


def make_provider(handler, api_key: str | None = "sk-test") -> OpenAICompatProvider:
    return OpenAICompatProvider(
        base_url="https://api.example.com/v1",
        model="gpt-test",
        api_key=api_key,
        transport=httpx.MockTransport(handler),
    )


def completion(content: str) -> dict:
    return {
        "model": "gpt-test",
        "choices": [{"message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 9, "completion_tokens": 2},
    }


async def test_generate_sends_openai_request_with_key():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=completion(" Hello! "))

    result = await make_provider(handler).generate(MESSAGES, temperature=0.3)

    assert captured["url"] == "https://api.example.com/v1/chat/completions"
    assert captured["auth"] == "Bearer sk-test"
    assert captured["body"] == {
        "model": "gpt-test",
        "messages": [{"role": "user", "content": "Hi"}],
        "stream": False,
        "temperature": 0.3,
    }
    assert result.content == "Hello!"
    assert result.prompt_tokens == 9
    assert result.completion_tokens == 2


async def test_no_key_means_no_auth_header():
    captured = {}

    def handler(request):
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json=completion("ok"))

    await make_provider(handler, api_key=None).generate(MESSAGES)

    assert captured["auth"] is None


async def test_stream_parses_server_sent_events():
    body = "\n".join(
        [
            'data: {"choices": [{"delta": {"role": "assistant"}}]}',
            "",
            'data: {"choices": [{"delta": {"content": "Hel"}}]}',
            'data: {"choices": [{"delta": {"content": "lo"}}]}',
            "data: [DONE]",
        ]
    )
    provider = make_provider(lambda r: httpx.Response(200, text=body))

    assert [t async for t in provider.stream(MESSAGES)] == ["Hel", "lo"]


async def test_bad_api_key_is_clear_and_not_retryable():
    provider = make_provider(lambda r: httpx.Response(401, json={"error": {"message": "bad"}}))

    with pytest.raises(LLMResponseError, match="rejected the API key") as exc_info:
        await provider.generate(MESSAGES)
    assert exc_info.value.retryable is False


@pytest.mark.parametrize(("status", "retryable"), [(429, True), (503, True), (400, False)])
async def test_status_codes_set_retryable(status, retryable):
    provider = make_provider(lambda r: httpx.Response(status, json={"error": {"message": "nope"}}))

    with pytest.raises(LLMResponseError, match="nope") as exc_info:
        await provider.generate(MESSAGES)
    assert exc_info.value.retryable is retryable


async def test_connection_error_is_unavailable():
    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(LLMUnavailableError):
        await make_provider(handler).generate(MESSAGES)


class City(BaseModel):
    name: str
    country: str


async def test_generate_structured_uses_json_schema():
    captured = {}

    def handler(request):
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=completion('{"name": "Singapore", "country": "SG"}'))

    result = await make_provider(handler).generate_structured(MESSAGES, City)

    assert result == City(name="Singapore", country="SG")
    response_format = captured["body"]["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["name"] == "City"
