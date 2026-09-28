"""What the agent reports while it works.

The orchestrator yields a stream of these events. The WebSocket turns them
into messages for the browser, so you can watch ARTHUR think, use tools
and answer - instead of waiting in the dark.
"""

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


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


class PlanEvent(BaseModel):
    """ARTHUR made a plan for a complex request."""

    type: Literal["plan"] = "plan"
    goal: str
    steps: list[dict[str, Any]]  # [{"id": 1, "task": "..."}]


class StepEvent(BaseModel):
    """A plan step changed status."""

    type: Literal["step"] = "step"
    id: int
    status: str  # running | done | failed | skipped
    attempt: int = 1
    detail: str = ""  # result preview or error


AgentEvent = TextEvent | ToolStartEvent | ToolEndEvent | ConfirmationEvent | PlanEvent | StepEvent


# ---------- task state (Phase 8) ----------


class StepStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


class StepState(BaseModel):
    id: int
    task: str
    status: StepStatus = StepStatus.PENDING
    attempts: int = 0
    result: str | None = None
    error: str | None = None


class TaskState(BaseModel):
    """Everything known about a multi-step task while it runs."""

    goal: str
    steps: list[StepState] = Field(default_factory=list)

    def completed_results(self) -> list[StepState]:
        return [s for s in self.steps if s.status == StepStatus.DONE]

    def report(self) -> str:
        """The step outcomes, written for the final-answer prompt."""
        lines = []
        for s in self.steps:
            if s.status == StepStatus.DONE:
                lines.append(f"Step {s.id} ({s.task}): {s.result}")
            else:
                reason = s.error or s.status.value
                lines.append(f"Step {s.id} ({s.task}): NOT COMPLETED - {reason}")
        return "\n".join(lines)
