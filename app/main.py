"""ARTHUR API entry point.

Run with:  uvicorn app.main:app --reload
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.errors import register_exception_handlers
from app.api.middleware import RequestContextMiddleware
from app.api.routes import chat, health
from app.config.settings import get_settings
from app.llm.factory import create_llm_provider
from app.observability.logging import configure_logging, get_logger

log = get_logger("arthur")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Code before `yield` runs at startup; code after it runs at shutdown."""
    settings = get_settings()
    configure_logging(settings.log_level, json_logs=settings.json_logs)
    app.state.llm = create_llm_provider(settings)
    log.info("arthur_started", provider=app.state.llm.name, model=app.state.llm.model)
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
    return app


app = create_app()
