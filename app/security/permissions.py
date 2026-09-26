"""Permission checks: may this tool run right now?

    Level 0 READ_ONLY   -> runs
    Level 1 LOW_RISK    -> runs
    Level 2 CONFIRM     -> only after the user explicitly confirms this call
    Level 3 SENSITIVE   -> never; the user must do it themselves

Tools can also be disabled by name (TOOLS_BLOCKED in .env) or limited to an
allow-list. Confirmation for level 2+ cannot be switched off by configuration.
"""

from dataclasses import dataclass
from enum import StrEnum

from app.tools.base import PermissionLevel, Tool


class Decision(StrEnum):
    ALLOWED = "allowed"
    NEEDS_CONFIRMATION = "needs_confirmation"
    DENIED = "denied"


@dataclass(frozen=True)
class PermissionDecision:
    decision: Decision
    reason: str = ""


class PermissionPolicy:
    def __init__(
        self,
        auto_approve_max_level: int = PermissionLevel.LOW_RISK,
        blocked_tools: set[str] | None = None,
        allowed_tools: set[str] | None = None,
    ) -> None:
        # Safety: even if configured higher, level 2+ always needs confirmation.
        self.auto_approve_max_level = min(auto_approve_max_level, PermissionLevel.LOW_RISK)
        self.blocked_tools = blocked_tools or set()
        self.allowed_tools = allowed_tools  # None = every registered tool

    def check(self, tool: Tool, *, confirmed: bool) -> PermissionDecision:
        if tool.name in self.blocked_tools:
            return PermissionDecision(Decision.DENIED, f"Tool '{tool.name}' is disabled.")
        if self.allowed_tools is not None and tool.name not in self.allowed_tools:
            return PermissionDecision(Decision.DENIED, f"Tool '{tool.name}' is not allowed.")
        if tool.permission_level >= PermissionLevel.SENSITIVE:
            return PermissionDecision(
                Decision.DENIED,
                "This is a highly sensitive action (money, passwords or accounts). "
                "ARTHUR never performs these - please do it yourself.",
            )
        if tool.permission_level > self.auto_approve_max_level and not confirmed:
            return PermissionDecision(Decision.NEEDS_CONFIRMATION, "Needs your confirmation.")
        return PermissionDecision(Decision.ALLOWED)
