"""What the agent reports while it works.

The orchestrator yields a stream of these events. The WebSocket turns them
into messages for the browser, so you can watch ARTHUR think, use tools
and answer - instead of waiting in the dark.
"""

from typing import Any, Literal

from pydantic import BaseModel


class TextEvent(BaseModel):
    """A piece of the answer."""

    type: Literal["text"] = "text"
    text: str


class ToolStartEvent(BaseModel):
    """ARTHUR is about to run a tool."""

    type: Literal["tool_start"] = "tool_start"
    call_id: str
    name: str
    arguments: dict[str, Any]


class ToolEndEvent(BaseModel):
    """A tool finished (successfully or not)."""

    type: Literal["tool_end"] = "tool_end"
    call_id: str
    name: str
    status: str  # ok | error | denied | needs_confirmation
    summary: str  # short, human-readable result
    duration_ms: float


class ConfirmationEvent(BaseModel):
    """A level-2 tool needs the user's "yes" before it can run."""

    type: Literal["confirmation"] = "confirmation"
    name: str
    arguments: dict[str, Any]
    preview: str


AgentEvent = TextEvent | ToolStartEvent | ToolEndEvent | ConfirmationEvent
