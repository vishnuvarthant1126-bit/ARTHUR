"""Tool endpoints: list tools, run one directly, read the audit log.

Running tools by hand is how we test them before the agent (Phase 7) uses
them automatically. Every run still goes through permissions and auditing.
"""

import asyncio
from typing import Any

import structlog
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.api.dependencies import ToolsDep
from app.security.audit import AuditEntry
from app.tools.base import ToolContext, ToolResult

router = APIRouter(tags=["tools"])


class ToolInfo(BaseModel):
    name: str
    description: str
    permission_level: int
    parameters: dict[str, Any]


class RunToolRequest(BaseModel):
    arguments: dict[str, Any] = Field(default_factory=dict, examples=[{"expression": "482 * 29"}])
    confirmed: bool = Field(
        default=False, description="Set true to approve a level-2 action after seeing its preview."
    )


@router.get("/tools", response_model=list[ToolInfo])
async def list_tools(tools: ToolsDep) -> list[ToolInfo]:
    return [
        ToolInfo(
            name=t.name,
            description=t.description,
            permission_level=int(t.permission_level),
            parameters=t.input_model.model_json_schema(),
        )
        for t in tools.all()
    ]


@router.post("/tools/{name}/run", response_model=ToolResult)
async def run_tool(name: str, body: RunToolRequest, tools: ToolsDep) -> ToolResult:
    if tools.get(name) is None:
        raise HTTPException(404, f"Unknown tool '{name}'.")
    request_id = structlog.contextvars.get_contextvars().get("request_id")
    context = ToolContext(request_id=request_id, confirmed=body.confirmed)
    return await tools.execute(name, body.arguments, context)


@router.get("/audit", response_model=list[AuditEntry])
async def audit_log(
    request: Request, limit: int = Query(default=50, ge=1, le=500)
) -> list[AuditEntry]:
    return await asyncio.to_thread(request.app.state.audit.recent, limit)
