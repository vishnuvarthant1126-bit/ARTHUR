"""The LLM provider interface.

The rest of ARTHUR talks only to `LLMProvider`, never to Ollama/OpenAI
directly. Adding a new provider = writing one new subclass; nothing else
changes.
"""

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from enum import StrEnum
from typing import Any, TypeVar

from pydantic import BaseModel, Field


class Role(StrEnum):
    SYSTEM = "system"  # instructions that shape ARTHUR's behaviour
    USER = "user"  # what the human said
    ASSISTANT = "assistant"  # what ARTHUR replied
    TOOL = "tool"  # the result of a tool ARTHUR used


class ToolCall(BaseModel):
    """The model asking ARTHUR to run a tool. It is only a request - nothing has run yet."""

    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class Message(BaseModel):
    role: Role
    content: str = ""
    # assistant messages: the tools the model asked for
    tool_calls: list[ToolCall] = Field(default_factory=list)
    # tool messages: which call this result answers, and the tool's name
    tool_call_id: str | None = None
    name: str | None = None


class LLMResponse(BaseModel):
    content: str
    model: str
    latency_ms: float
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)


# --- Streaming events: a reply is a series of text pieces, possibly ending in tool calls ---


class TextDelta(BaseModel):
    text: str


class ToolCallsRequested(BaseModel):
    calls: list[ToolCall]


StreamEvent = TextDelta | ToolCallsRequested


# --- Errors: provider-specific failures are translated into these ---


class LLMError(Exception):
    """Base class for every LLM failure.

    `retryable` says whether trying again might succeed (a brief outage, rate
    limit) or is pointless (bad API key, model not installed).
    """

    default_retryable = False

    def __init__(self, message: str, *, retryable: bool | None = None) -> None:
        super().__init__(message)
        self.retryable = self.default_retryable if retryable is None else retryable


class LLMUnavailableError(LLMError):
    """The provider could not be reached (not running, no network)."""

    default_retryable = True


class LLMTimeoutError(LLMError):
    """The provider did not answer in time. Not retried: it would double the wait."""


class LLMResponseError(LLMError):
    """The provider answered with an error or unusable output."""


T = TypeVar("T", bound=BaseModel)


class LLMProvider(ABC):
    name: str
    model: str

    @abstractmethod
    async def generate(
        self,
        messages: list[Message],
        *,
        temperature: float | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        """Return the complete reply in one piece. With `tools`, the reply may be tool calls."""

    @abstractmethod
    def stream(
        self, messages: list[Message], *, temperature: float | None = None
    ) -> AsyncIterator[str]:
        """Yield the reply piece by piece (tokens) as it is generated."""

    async def stream_chat(
        self,
        messages: list[Message],
        *,
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Stream a reply that may end in tool calls.

        Default: without tools, plain streaming; with tools, one non-streamed call.
        Providers that can stream tool calls (Ollama) override this.
        """
        if not tools:
            async for token in self.stream(messages, temperature=temperature):
                yield TextDelta(text=token)
            return
        response = await self.generate(messages, temperature=temperature, tools=tools)
        if response.content:
            yield TextDelta(text=response.content)
        if response.tool_calls:
            yield ToolCallsRequested(calls=response.tool_calls)

    @abstractmethod
    async def generate_structured(self, messages: list[Message], schema: type[T]) -> T:
        """Return a reply parsed and validated into the given Pydantic model."""

    async def health(self) -> bool:
        """True if the provider is reachable. Override when a cheap check exists."""
        return True

    async def aclose(self) -> None:  # noqa: B027 - optional hook, no-op by default
        """Release network connections on shutdown."""
