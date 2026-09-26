"""Tool registry: the single gate every tool call goes through.

request (name + JSON arguments)
  -> find the tool                 unknown name        -> error
  -> permission check              blocked / level 3   -> denied
  -> validate arguments (Pydantic) wrong types/missing -> error
  -> confirmation needed?          level 2, no "yes"   -> needs_confirmation + preview
  -> run with timeout              too slow            -> error
  -> catch every exception         crash               -> error (ARTHUR keeps running)
  -> audit log (always, whatever the outcome)
"""

import asyncio
import time
from typing import Any

from pydantic import ValidationError

from app.observability.logging import get_logger
from app.security.audit import AuditLog
from app.security.permissions import Decision, PermissionPolicy
from app.tools.base import Tool, ToolContext, ToolError, ToolResult

log = get_logger(__name__)


class ToolRegistry:
    def __init__(
        self,
        policy: PermissionPolicy,
        audit: AuditLog | None = None,
        default_timeout_seconds: float = 10.0,
    ) -> None:
        self.policy = policy
        self.audit = audit
        self.default_timeout_seconds = default_timeout_seconds
        self._tools: dict[str, Tool] = {}

    # ---------- registration ----------

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"Tool '{tool.name}' is already registered.")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def all(self) -> list[Tool]:
        return list(self._tools.values())

    def llm_schemas(self) -> list[dict[str, Any]]:
        """Schemas of the tools the LLM may request (blocked tools are hidden)."""
        return [
            t.llm_schema() for t in self._tools.values() if t.name not in self.policy.blocked_tools
        ]

    # ---------- execution ----------

    async def execute(
        self, name: str, arguments: dict[str, Any] | None, context: ToolContext | None = None
    ) -> ToolResult:
        context = context or ToolContext()
        arguments = arguments or {}
        start = time.perf_counter()

        tool = self._tools.get(name)
        if tool is None:
            known = ", ".join(sorted(self._tools)) or "none"
            return await self._finish(
                name, arguments, -1, "denied", start, context,
                status="error", error=f"Unknown tool '{name}'. Available tools: {known}.",
            )  # fmt: skip

        decision = self.policy.check(tool, confirmed=context.confirmed)
        level = int(tool.permission_level)
        if decision.decision == Decision.DENIED:
            return await self._finish(
                name, arguments, level, "denied", start, context,
                status="denied", error=decision.reason,
            )  # fmt: skip

        try:
            args = tool.input_model.model_validate(arguments)
        except ValidationError as exc:
            problems = "; ".join(
                f"{'.'.join(map(str, e['loc'])) or 'input'}: {e['msg']}" for e in exc.errors()
            )
            return await self._finish(
                name, arguments, level, "allowed", start, context,
                status="error", error=f"Invalid arguments - {problems}",
            )  # fmt: skip

        if decision.decision == Decision.NEEDS_CONFIRMATION:
            return await self._finish(
                name, arguments, level, "needs_confirmation", start, context,
                status="needs_confirmation", preview=tool.preview(args),
            )  # fmt: skip

        timeout = tool.timeout_seconds or self.default_timeout_seconds
        try:
            output = await asyncio.wait_for(tool.run(args, context), timeout=timeout)
        except TimeoutError:
            error = f"Tool '{name}' timed out after {timeout:g}s."
        except ToolError as exc:
            error = str(exc)
        except Exception:
            log.exception("tool_crashed", tool=name)
            error = f"Tool '{name}' failed unexpectedly."
        else:
            return await self._finish(
                name, arguments, level, "allowed", start, context, status="ok", output=output
            )
        return await self._finish(
            name, arguments, level, "allowed", start, context, status="error", error=error
        )

    async def _finish(
        self,
        name: str,
        arguments: dict[str, Any],
        level: int,
        decision: str,
        start: float,
        context: ToolContext,
        *,
        status: str,
        output: Any = None,
        error: str | None = None,
        preview: str | None = None,
    ) -> ToolResult:
        duration_ms = round((time.perf_counter() - start) * 1000, 1)
        result = ToolResult(
            tool=name, status=status, output=output, error=error,
            preview=preview, duration_ms=duration_ms,
        )  # fmt: skip
        log.info(
            "tool_call", tool=name, status=status, decision=decision,
            level=level, duration_ms=duration_ms, error=error,
        )  # fmt: skip
        if self.audit:
            try:
                await asyncio.to_thread(
                    self.audit.record,
                    tool=name, arguments=arguments, permission_level=level,
                    decision=decision, success=result.ok, error=error,
                    duration_ms=duration_ms, request_id=context.request_id,
                )  # fmt: skip
            except Exception:
                log.exception("audit_write_failed", tool=name)
        return result
