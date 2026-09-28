"""The agent loop: let the LLM use tools until it can answer.

    repeat (at most max_steps times, within max_seconds):
        ask the LLM, offering the tool menu
        no tool calls?  -> that text is the final answer, stop
        tool calls?     -> run each through the ToolRegistry (permissions, validation,
                           timeout, audit), add the results to the conversation, loop

Safety:
- The LLM only *requests* tools; the registry decides and runs them.
- Hard limits on steps and time: no infinite loops, whatever the model does.
- Repeating an identical call is refused (the model is told to use the earlier result).
- A tool that needs confirmation stops the loop; only the user can say yes.
- Tool results are wrapped and labelled as data, never as instructions.
"""

import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass

from app.agent.state import (
    AgentEvent,
    ConfirmationEvent,
    TextEvent,
    ToolEndEvent,
    ToolStartEvent,
)
from app.llm.base import LLMProvider, Message, Role, TextDelta, ToolCall
from app.observability.logging import get_logger
from app.tools.base import ToolContext, ToolResult
from app.tools.registry import ToolRegistry

log = get_logger(__name__)


@dataclass(frozen=True)
class AgentLimits:
    max_steps: int = 8  # LLM rounds per request
    max_seconds: float = 120.0  # wall-clock budget per request
    max_result_chars: int = 4000  # tool output shown to the LLM


class ToolLoop:
    def __init__(
        self,
        llm: LLMProvider,
        registry: ToolRegistry,
        *,
        tool_names: set[str] | None = None,
        limits: AgentLimits | None = None,
    ) -> None:
        self.llm = llm
        self.registry = registry
        self.tool_names = tool_names  # None = every allowed tool
        self.limits = limits or AgentLimits()

    async def run(self, messages: list[Message], context: ToolContext) -> AsyncIterator[AgentEvent]:
        messages = list(messages)  # never modify the caller's list
        tools = self.registry.llm_schemas(self.tool_names)
        deadline = time.monotonic() + self.limits.max_seconds
        seen_calls: set[str] = set()

        for step in range(1, self.limits.max_steps + 1):
            if time.monotonic() > deadline:
                log.warning("agent_time_limit", step=step)
                yield TextEvent(
                    text="\n\n(I stopped because this took too long. Try a simpler request.)"
                )
                return

            text_parts: list[str] = []
            calls: list[ToolCall] = []
            async for event in self.llm.stream_chat(messages, tools=tools):
                if isinstance(event, TextDelta):
                    text_parts.append(event.text)
                    yield TextEvent(text=event.text)
                else:
                    calls.extend(event.calls)

            if not calls:
                log.info("agent_finished", steps=step)
                return  # the streamed text was the final answer

            messages.append(
                Message(role=Role.ASSISTANT, content="".join(text_parts), tool_calls=calls)
            )
            for call in calls:
                fingerprint = f"{call.name}:{json.dumps(call.arguments, sort_keys=True)}"
                if fingerprint in seen_calls:
                    messages.append(
                        self._tool_message(
                            call,
                            {
                                "status": "error",
                                "error": "You already made this exact call. "
                                "Use the earlier result instead of repeating it.",
                            },
                        )  # fmt: skip
                    )
                    continue
                seen_calls.add(fingerprint)

                yield ToolStartEvent(call_id=call.id, name=call.name, arguments=call.arguments)
                result = await self.registry.execute(call.name, call.arguments, context)
                yield ToolEndEvent(
                    call_id=call.id,
                    name=call.name,
                    status=result.status,
                    summary=self._summarize(result),
                    duration_ms=result.duration_ms,
                )

                if result.status == "needs_confirmation":
                    # Stop here: only the user can approve this. The orchestrator asks them.
                    yield ConfirmationEvent(
                        name=call.name, arguments=call.arguments, preview=result.preview or ""
                    )
                    return
                messages.append(self._tool_message(call, self._result_payload(result)))

        log.warning("agent_step_limit", max_steps=self.limits.max_steps)
        yield TextEvent(
            text=f"\n\n(I stopped after {self.limits.max_steps} steps without finishing. "
            "Try breaking the request into smaller parts.)"
        )

    def _result_payload(self, result: ToolResult) -> dict:
        if result.ok:
            return {"status": "ok", "output": result.output}
        return {"status": result.status, "error": result.error}

    def _tool_message(self, call: ToolCall, payload: dict) -> Message:
        text = json.dumps(payload, ensure_ascii=False, default=str)
        if len(text) > self.limits.max_result_chars:
            text = text[: self.limits.max_result_chars] + " ...(truncated)"
        return Message(role=Role.TOOL, name=call.name, tool_call_id=call.id, content=text)

    def _summarize(self, result: ToolResult, limit: int = 140) -> str:
        """One short line for the UI, e.g. '482 * 29 = 13978' or the error message."""
        if result.status == "needs_confirmation":
            return f"waiting for your confirmation: {result.preview}"
        if not result.ok:
            return result.error or result.status
        tool = self.registry.get(result.tool)
        try:
            text = tool.summarize(result.output) if tool else str(result.output)
        except (KeyError, TypeError, AttributeError):
            text = json.dumps(result.output, ensure_ascii=False, default=str)
        return text if len(text) <= limit else text[: limit - 1] + "…"
