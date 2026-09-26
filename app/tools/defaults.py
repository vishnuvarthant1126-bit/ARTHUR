"""Build the registry with ARTHUR's built-in tools."""

import httpx

from app.memory.manager import MemoryManager
from app.security.audit import AuditLog
from app.security.permissions import PermissionPolicy
from app.tools.calculator import CalculatorTool
from app.tools.memory_tools import DeleteMemoryTool, SaveMemoryTool, SearchMemoryTool
from app.tools.registry import ToolRegistry
from app.tools.time_tool import CurrentTimeTool
from app.tools.weather import WeatherTool


def create_tool_registry(
    *,
    policy: PermissionPolicy,
    audit: AuditLog | None,
    http_client: httpx.AsyncClient,
    memory: MemoryManager | None,
    default_timeout_seconds: float = 10.0,
    memory_min_score: float = 0.55,
) -> ToolRegistry:
    registry = ToolRegistry(policy, audit, default_timeout_seconds)
    registry.register(CalculatorTool())
    registry.register(CurrentTimeTool())
    registry.register(WeatherTool(http_client))
    if memory is not None:
        registry.register(SearchMemoryTool(memory, memory_min_score))
        registry.register(SaveMemoryTool(memory))
        registry.register(DeleteMemoryTool(memory))
    return registry
