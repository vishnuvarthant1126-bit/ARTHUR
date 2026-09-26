"""The LLM provider interface.

The rest of ARTHUR talks only to `LLMProvider`, never to Ollama/OpenAI
directly. Adding a new provider = writing one new subclass; nothing else
changes.
"""

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from enum import StrEnum
from typing import TypeVar

from pydantic import BaseModel


class Role(StrEnum):
    SYSTEM = "system"  # instructions that shape ARTHUR's behaviour
    USER = "user"  # what the human said
    ASSISTANT = "assistant"  # what ARTHUR replied
    TOOL = "tool"  # tool results (Phase 7)


class Message(BaseModel):
    role: Role
    content: str


class LLMResponse(BaseModel):
    content: str
    model: str
    latency_ms: float
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


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
        self, messages: list[Message], *, temperature: float | None = None
    ) -> LLMResponse:
        """Return the complete reply in one piece."""

    @abstractmethod
    def stream(
        self, messages: list[Message], *, temperature: float | None = None
    ) -> AsyncIterator[str]:
        """Yield the reply piece by piece (tokens) as it is generated."""

    @abstractmethod
    async def generate_structured(self, messages: list[Message], schema: type[T]) -> T:
        """Return a reply parsed and validated into the given Pydantic model."""

    async def health(self) -> bool:
        """True if the provider is reachable. Override when a cheap check exists."""
        return True

    async def aclose(self) -> None:  # noqa: B027 - optional hook, no-op by default
        """Release network connections on shutdown."""
