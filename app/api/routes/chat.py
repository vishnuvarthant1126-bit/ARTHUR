"""POST /chat - send one message to ARTHUR, get one reply.

Pass the returned `session_id` back on the next request and ARTHUR
remembers the conversation. Omit it to start a new one.
"""

import structlog
from fastapi import APIRouter, Request, Response, status
from pydantic import BaseModel, Field, field_validator

from app.api.dependencies import SESSION_ID_PATTERN, OrchestratorDep, new_session_id
from app.observability.logging import get_logger

router = APIRouter(tags=["chat"])
log = get_logger(__name__)

MAX_MESSAGE_CHARS = 8000


class MessageIn(BaseModel):
    message: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS, examples=["Hello Arthur"])

    @field_validator("message")
    @classmethod
    def not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("message must not be empty")
        return value


class ChatRequest(MessageIn):
    session_id: str | None = Field(default=None, pattern=SESSION_ID_PATTERN)


class ToolUse(BaseModel):
    name: str
    status: str
    summary: str


class ChatResponse(BaseModel):
    response: str
    session_id: str
    model: str
    latency_ms: float
    tools_used: list[ToolUse] = []


@router.post("/chat", response_model=ChatResponse)
async def chat(body: ChatRequest, orchestrator: OrchestratorDep, request: Request) -> ChatResponse:
    session_id = body.session_id or new_session_id()
    request_id = structlog.contextvars.get_contextvars().get("request_id")
    metrics = getattr(request.app.state, "metrics", None)
    try:
        result = await orchestrator.respond(session_id, body.message, request_id)
    except Exception:
        if metrics is not None:
            metrics.chat_turns.labels("http", "error").inc()
        raise
    if metrics is not None:
        metrics.chat_turns.labels("http", "ok").inc()
    log.info(
        "chat_completed",
        model=result.model,
        latency_ms=result.latency_ms,
        tools=[t.name for t in result.tools_used],
    )
    return ChatResponse(
        response=result.content,
        session_id=session_id,
        model=result.model,
        latency_ms=result.latency_ms,
        tools_used=[
            ToolUse(name=t.name, status=t.status, summary=t.summary) for t in result.tools_used
        ],
    )


@router.delete(
    "/chat/{session_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Forget a conversation",
)
async def clear_chat(session_id: str, orchestrator: OrchestratorDep) -> Response:
    orchestrator.clear(session_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
