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
                "parameters": slim_schema(self.input_model.model_json_schema()),
            },
        }


_NOISE = {"title", "minLength", "maxLength", "$defs"}


def slim_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Shorten a Pydantic JSON schema for the model.

    Every tool description is sent with every request and the model has to read it. The
    model needs names, types, descriptions and choices - not a "title" per field, length
    limits (ARTHUR validates the arguments itself anyway) or the roundabout way JSON
    Schema spells "optional". This only changes what the MODEL is shown.
    """
    definitions = schema.get("$defs", {})

    def clean(node: Any, inside_properties: bool = False) -> Any:
        if isinstance(node, list):
            return [clean(item) for item in node]
        if not isinstance(node, dict):
            return node
        if inside_properties:  # keys are parameter NAMES here (one may be called "title")
            return {name: clean(value) for name, value in node.items()}
        if "$ref" in node:  # e.g. an enum defined once under $defs: put it in place
            target = definitions.get(node["$ref"].rsplit("/", 1)[-1], {})
            node = {**target, **{k: v for k, v in node.items() if k != "$ref"}}
        options = node.get("anyOf")
        if options and {"type": "null"} in options:  # "string or nothing" -> just "string"
            others = [option for option in options if option != {"type": "null"}]
            if len(others) == 1:
                node = {**others[0], **{k: v for k, v in node.items() if k != "anyOf"}}
                if node.get("default") is None:
                    node.pop("default", None)
        return {
            key: clean(value, inside_properties=key == "properties")
            for key, value in node.items()
            if key not in _NOISE
        }

    return clean(schema)
