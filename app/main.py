"""ARTHUR API entry point.

Run with:  uvicorn app.main:app --reload
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.agent.orchestrator import Orchestrator
from app.api import websocket
from app.api.errors import register_exception_handlers
from app.api.middleware import RequestContextMiddleware
from app.api.routes import chat, health
from app.config.settings import Settings, get_settings
from app.llm.base import LLMProvider
from app.llm.factory import create_llm_provider
from app.memory.short_term import ConversationStore
from app.observability.logging import configure_logging, get_logger

log = get_logger("arthur")

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"


def build_orchestrator(llm: LLMProvider, settings: Settings) -> Orchestrator:
    conversations = ConversationStore(
        max_sessions=settings.memory_max_sessions,
        ttl_seconds=settings.memory_session_ttl_minutes * 60,
    )
    return Orchestrator(
        llm,
        conversations,
        context_tokens=settings.llm_context_tokens,
        reply_reserve_tokens=settings.llm_reply_reserve_tokens,
        max_history_messages=settings.memory_max_history_messages,
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Code before `yield` runs at startup; code after it runs at shutdown."""
    settings = get_settings()
    configure_logging(settings.log_level, json_logs=settings.json_logs)
    llm = create_llm_provider(settings)
    app.state.llm = llm
    app.state.orchestrator = build_orchestrator(llm, settings)
    log.info(
        "arthur_started",
        provider=llm.name,
        model=llm.model,
        fallback=settings.llm_fallback_provider,
        context_tokens=settings.llm_context_tokens,
    )
    yield
    await app.state.llm.aclose()
    log.info("arthur_stopped")


def create_app() -> FastAPI:
    app = FastAPI(
        title="ARTHUR",
        description="Personal Multimodal AI Agent",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.add_middleware(RequestContextMiddleware)
    register_exception_handlers(app)
    app.include_router(health.router)
    app.include_router(chat.router)
    app.include_router(websocket.router)
    # Mounted last: API routes above win; everything else is served from frontend/.
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
    return app


app = create_app()
