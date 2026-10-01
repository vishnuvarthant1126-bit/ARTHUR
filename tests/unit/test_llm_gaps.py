"""Gaps found by the coverage report in the language-model layer.

The OpenAI-compatible provider is ARTHUR's fallback: it must speak tool calls correctly
even though the day-to-day provider is Ollama.
"""

import json

import httpx
import pytest
from pydantic import BaseModel

from app.llm.base import (
    LLMProvider,
    LLMResponse,
    LLMResponseError,
    LLMUnavailableError,
    Message,
    Role,
    TextDelta,
    ToolCall,
    ToolCallsRequested,
)
from app.llm.metered import MeteredProvider
from app.llm.openai_compat import OpenAICompatProvider
from app.observability.metrics import Metrics
from tests.conftest import FakeLLM

MESSAGES = [Message(role=Role.USER, content="What is 6 times 7?")]
TOOLS = [{"type": "function", "function": {"name": "calculator", "parameters": {}}}]


def openai(handler) -> OpenAICompatProvider:
    return OpenAICompatProvider(
        base_url="https://api.example.com/v1",
        model="gpt-test",
        api_key="sk-test",
        transport=httpx.MockTransport(handler),
    )


def tool_reply(arguments: str) -> dict:
    call = {
        "id": "call_1",
        "type": "function",
        "function": {"name": "calculator", "arguments": arguments},
    }
    return {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [call]}}]}


# ---------- OpenAI-compatible provider: tool calling ----------


async def test_tool_calls_are_parsed_from_json_strings():
    sent = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent.update(json.loads(request.content))
        return httpx.Response(200, json=tool_reply('{"expression": "6 * 7"}'))

    response = await openai(handler).generate(MESSAGES, tools=TOOLS)

    assert sent["tools"] == TOOLS
    assert response.content == ""
    assert response.tool_calls == [
        ToolCall(id="call_1", name="calculator", arguments={"expression": "6 * 7"})
    ]


@pytest.mark.parametrize("arguments", ["{not json", '"just a string"', "[1, 2]", ""])
async def test_broken_tool_arguments_become_empty_not_a_crash(arguments):
    """The registry then reports the missing arguments to the model, which can try again."""
    provider = openai(lambda request: httpx.Response(200, json=tool_reply(arguments)))
    response = await provider.generate(MESSAGES, tools=TOOLS)
    assert response.tool_calls[0].arguments == {}


async def test_tool_results_are_sent_back_in_openai_format():
    sent = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent.update(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "It is 42."}}]})

    history = [
        *MESSAGES,
        Message(
            role=Role.ASSISTANT,
            content="",
            tool_calls=[
                ToolCall(id="call_1", name="calculator", arguments={"expression": "6 * 7"})
            ],
        ),
        Message(role=Role.TOOL, content='{"result": 42}', tool_call_id="call_1", name="calculator"),
    ]
    await openai(handler).generate(history)

    assistant, tool = sent["messages"][1], sent["messages"][2]
    assert assistant["content"] is None
    # OpenAI expects the arguments as a JSON *string*, not an object:
    assert assistant["tool_calls"][0]["function"] == {
        "name": "calculator",
        "arguments": '{"expression": "6 * 7"}',
    }
    assert tool == {"role": "tool", "content": '{"result": 42}', "tool_call_id": "call_1"}


async def test_health_and_unexpected_answers():
    assert await openai(lambda request: httpx.Response(200, json={"data": []})).health()
    assert not await openai(lambda request: httpx.Response(500)).health()

    def offline(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    assert not await openai(offline).health()

    with pytest.raises(LLMResponseError, match="Unexpected response shape"):
        await openai(lambda request: httpx.Response(200, json={"choices": []})).generate(MESSAGES)
    with pytest.raises(LLMResponseError, match="not found"):
        await openai(lambda request: httpx.Response(404)).generate(MESSAGES)


async def test_stream_reports_errors_and_skips_noise():
    def handler(request: httpx.Request) -> httpx.Response:
        body = (
            ": keep-alive comment\n\n"
            'data: {"choices": [{"delta": {"content": "Hel"}}]}\n\n'
            'data: {"choices": [{"delta": {}}]}\n\n'
            'data: {"error": {"message": "overloaded"}}\n\n'
        )
        return httpx.Response(200, text=body)

    tokens = []
    with pytest.raises(LLMResponseError, match="overloaded"):
        async for token in openai(handler).stream(MESSAGES):
            tokens.append(token)
    assert tokens == ["Hel"]

    with pytest.raises(LLMResponseError):
        async for _ in openai(lambda request: httpx.Response(401)).stream(MESSAGES):
            pass


async def test_structured_output_that_does_not_fit_the_schema():
    class Plan(BaseModel):
        steps: list[str]

    provider = openai(
        lambda request: httpx.Response(
            200, json={"choices": [{"message": {"content": '{"steps": "not a list"}'}}]}
        )
    )
    with pytest.raises(LLMResponseError, match="did not match Plan"):
        await provider.generate_structured(MESSAGES, Plan)


# ---------- the default stream_chat (providers that can't stream tool calls) ----------


class PlainProvider(LLMProvider):
    """A provider with only the required methods - it inherits the default stream_chat."""

    name = "plain"
    model = "plain-model"

    def __init__(self, response: LLMResponse) -> None:
        self.response = response

    async def generate(self, messages, *, temperature=None, tools=None) -> LLMResponse:
        return self.response

    async def stream(self, messages, *, temperature=None):
        for word in ("Hello", " there"):
            yield word

    async def generate_structured(self, messages, schema):
        raise NotImplementedError


async def test_default_stream_chat_without_tools_streams_text():
    provider = PlainProvider(LLMResponse(content="unused", model="plain-model", latency_ms=1))
    events = [event async for event in provider.stream_chat(MESSAGES)]
    assert events == [TextDelta(text="Hello"), TextDelta(text=" there")]
    assert await provider.health()  # the default: assume reachable


async def test_default_stream_chat_with_tools_uses_one_complete_call():
    call = ToolCall(id="1", name="calculator", arguments={"expression": "6 * 7"})
    provider = PlainProvider(
        LLMResponse(content="Let me check.", model="plain-model", latency_ms=1, tool_calls=[call])
    )
    events = [event async for event in provider.stream_chat(MESSAGES, tools=TOOLS)]
    assert events == [TextDelta(text="Let me check."), ToolCallsRequested(calls=[call])]


# ---------- the metering wrapper: remaining paths ----------


async def test_metered_structured_calls_and_failures():
    class Plan(BaseModel):
        goal: str

    metrics = Metrics()
    llm = MeteredProvider(FakeLLM(structured_reply='{"goal": "tea"}'), metrics)

    assert (await llm.generate_structured(MESSAGES, Plan)).goal == "tea"
    assert await llm.health()

    failing = MeteredProvider(FakeLLM(error=LLMUnavailableError("Ollama is not running")), metrics)
    with pytest.raises(LLMUnavailableError):
        await failing.generate_structured(MESSAGES, Plan)
    with pytest.raises(LLMUnavailableError):
        async for _ in failing.stream(MESSAGES):
            pass

    def count(kind: str, outcome: str) -> float:
        labels = {"model": llm.model, "kind": kind, "outcome": outcome}
        return metrics.registry.get_sample_value("arthur_llm_requests_total", labels) or 0

    assert count("structured", "ok") == 1
    assert count("structured", "error") == 1
    assert count("stream", "error") == 1
    await llm.aclose()
