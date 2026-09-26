"""POST /chat - send one message to ARTHUR, get one reply."""

from fastapi import APIRouter
from pydantic import BaseModel, Field, field_validator

from app.agent.prompts import build_chat_messages
from app.api.dependencies import LLMDep
from app.observability.logging import get_logger

router = APIRouter(tags=["chat"])
log = get_logger(__name__)

MAX_MESSAGE_CHARS = 8000


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS, examples=["Hello Arthur"])

    @field_validator("message")
    @classmethod
    def not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("message must not be empty")
        return value


class ChatResponse(BaseModel):
    response: str
    model: str
    latency_ms: float


@router.post("/chat", response_model=ChatResponse)
async def chat(body: ChatRequest, llm: LLMDep) -> ChatResponse:
    result = await llm.generate(build_chat_messages(body.message))
    log.info(
        "chat_completed",
        model=result.model,
        llm_latency_ms=result.latency_ms,
        prompt_tokens=result.prompt_tokens,
        completion_tokens=result.completion_tokens,
    )
    return ChatResponse(response=result.content, model=result.model, latency_ms=result.latency_ms)
