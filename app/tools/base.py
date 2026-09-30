"""The Tool interface.

A tool is a Python function ARTHUR is allowed to use: calculator, clock,
weather, memory... The LLM can only *ask* for a tool by name with JSON
arguments; the ToolRegistry decides whether it may run, validates the
arguments, runs it with a timeout and records it in the audit log.

Every tool declares:
    name              unique id the LLM uses, e.g. "calculator"
    description       tells the LLM when to use it
    input_model       Pydantic model = the input schema (validated before running)
    permission_level  how risky it is (see PermissionLevel)
    timeout_seconds   maximum run time
    run()             the actual work
"""

import json
from abc import ABC, abstractmethod
from collections.abc import Awaitable
from dataclasses import dataclass
from enum import IntEnum
from typing import Any, ClassVar, Literal

from pydantic import BaseModel


class PermissionLevel(IntEnum):
    READ_ONLY = 0  # reading, calculating, looking things up
    LOW_RISK = 1  # small local changes that are easy to undo (e.g. save a memory)
    CONFIRM = 2  # needs the user's explicit "yes" (delete, send, submit)
    SENSITIVE = 3  # money, passwords, accounts - ARTHUR never does these


class ToolError(Exception):
    """An expected failure with a message that is safe to show the user."""


@dataclass
class ToolContext:
    request_id: str | None = None
    session_id: str | None = None
    confirmed: bool = False  # the user explicitly approved this exact call


class ToolResult(BaseModel):
    tool: str
    status: Literal["ok", "error", "needs_confirmation", "denied"]
    output: Any = None
    error: str | None = None
    preview: str | None = None  # what will happen - shown when asking for confirmation
    duration_ms: float = 0.0

    @property
    def ok(self) -> bool:
        return self.status == "ok"


class Tool[InputT: BaseModel](ABC):
    name: ClassVar[str]
    description: ClassVar[str]
    input_model: ClassVar[type[BaseModel]]
    permission_level: ClassVar[PermissionLevel] = PermissionLevel.READ_ONLY
    timeout_seconds: ClassVar[float | None] = None  # None = registry default

    @abstractmethod
    async def run(self, args: InputT, context: ToolContext) -> Any:
        """Do the work and return JSON-serialisable output. Raise ToolError on expected failures."""

    def preview(self, args: InputT) -> str | Awaitable[str]:
        """Human-readable description of this call, shown before asking for confirmation.

        May be `async` when the description needs a lookup (e.g. a memory's text).
        """
        return f"{self.name}({args.model_dump_json()})"

    def required_level(self, args: InputT) -> PermissionLevel | Awaitable[PermissionLevel]:
        """The permission level of THIS call. Override to make some calls riskier than others
        (e.g. clicking "Buy now" needs confirmation, clicking "Search" doesn't).
        Can never lower the tool's own `permission_level`. May be `async`."""
        return self.permission_level

    def summarize(self, output: Any) -> str:
        """One short line describing a successful result, shown in the UI."""
        return json.dumps(output, ensure_ascii=False, default=str)

    def llm_schema(self) -> dict[str, Any]:
        """Description in the standard function-calling format understood by Ollama/OpenAI."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_model.model_json_schema(),
            },
        }
