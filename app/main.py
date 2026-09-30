"""ARTHUR API entry point.

Run with:  uvicorn app.main:app --reload
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.agent.executor import AgentLimits, PlanLimits
from app.agent.orchestrator import Orchestrator
from app.api import websocket
from app.api.errors import register_exception_handlers
from app.api.middleware import RequestContextMiddleware
from app.api.routes import chat, health, memory, tools, voice
from app.api.routes import documents as documents_routes
from app.config.settings import Settings, get_settings
from app.database.database import Database
from app.files.workspace import Workspace
from app.llm.base import LLMProvider
from app.llm.factory import create_llm_provider
from app.memory.long_term import MemoryRepository
from app.memory.manager import MemoryManager
from app.memory.short_term import ConversationStore
from app.memory.vector_store import ChromaVectorStore
from app.observability.logging import configure_logging, get_logger
from app.rag.documents import DocumentService
from app.rag.embeddings import create_embedding_provider
from app.rag.retrieval import DocumentRetriever
from app.search.providers import create_search_provider
from app.search.service import WebSearchService
from app.security.audit import AuditLog
from app.security.permissions import PermissionPolicy
from app.tools.defaults import create_tool_registry
from app.tools.registry import ToolRegistry
from app.voice.speech_to_text import WhisperSTT
from app.voice.text_to_speech import PiperTTS

log = get_logger("arthur")

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"


def build_orchestrator(
    llm: LLMProvider,
    settings: Settings,
    memory: MemoryManager | None = None,
    tools: ToolRegistry | None = None,
    retriever: DocumentRetriever | None = None,
) -> Orchestrator:
    conversations = ConversationStore(
        max_sessions=settings.memory_max_sessions,
        ttl_seconds=settings.memory_session_ttl_minutes * 60,
    )
    return Orchestrator(
        llm,
        conversations,
        memory=memory,
        tools=tools,
        agent_limits=AgentLimits(
            max_steps=settings.agent_max_steps, max_seconds=settings.agent_max_seconds
        ),
        planning=settings.agent_planning,
        plan_limits=PlanLimits(max_seconds=settings.agent_plan_max_seconds),
        context_tokens=settings.llm_context_tokens,
        reply_reserve_tokens=settings.llm_reply_reserve_tokens,
        max_history_messages=settings.memory_max_history_messages,
        memory_top_k=settings.memory_top_k,
        memory_min_score=settings.memory_min_score,
        retriever=retriever,
        rag_top_k=settings.rag_top_k,
        rag_min_score=settings.rag_min_score,
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Code before `yield` runs at startup; code after it runs at shutdown."""
    settings = get_settings()
    configure_logging(settings.log_level, json_logs=settings.json_logs)

    db = Database(settings.resolve(settings.database_path))
    db.create_tables()

    llm = create_llm_provider(settings)
    embeddings = create_embedding_provider(
        settings.embedding_provider, settings.ollama_base_url, settings.embedding_model
    )
    memory_manager = MemoryManager(
        MemoryRepository(db),
        ChromaVectorStore(settings.resolve(settings.vector_store_path)),
        embeddings,
    )
    document_vectors = ChromaVectorStore(
        settings.resolve(settings.vector_store_path), collection="documents"
    )
    documents = DocumentService(
        db,
        document_vectors,
        embeddings,
        settings.resolve(settings.documents_path),
        max_bytes=settings.documents_max_mb * 1024 * 1024,
        chunk_size=settings.rag_chunk_size,
        chunk_overlap=settings.rag_chunk_overlap,
    )
    retriever = DocumentRetriever(document_vectors, embeddings)
    audit = AuditLog(db)
    http_client = httpx.AsyncClient(headers={"User-Agent": "ARTHUR/0.1 (personal assistant)"})
    search = WebSearchService(
        create_search_provider(
            settings.search_provider, searxng_url=settings.searxng_url, client=http_client
        ),
        max_per_minute=settings.search_rate_limit_per_minute,
        cache_seconds=settings.search_cache_minutes * 60,
    )

    app.state.llm = llm
    app.state.memory = memory_manager
    app.state.documents = documents
    app.state.retriever = retriever
    app.state.audit = audit
    app.state.tools = create_tool_registry(
        policy=PermissionPolicy(
            auto_approve_max_level=settings.tools_auto_approve_max_level,
            blocked_tools=settings.blocked_tools,
        ),
        audit=audit,
        http_client=http_client,
        memory=memory_manager,
        documents=documents,
        retriever=retriever,
        search=search,
        web_fetch_max_bytes=settings.web_fetch_max_kb * 1024,
        workspace=Workspace(settings.file_roots, settings.files_save_dir),
        default_timeout_seconds=settings.tools_default_timeout_seconds,
        memory_min_score=settings.memory_min_score,
        document_min_score=settings.rag_min_score,
    )
    app.state.orchestrator = build_orchestrator(
        llm, settings, memory_manager, app.state.tools, retriever
    )
    app.state.stt = WhisperSTT(
        settings.whisper_model,
        settings.whisper_device,
        settings.whisper_compute_type,
        language=settings.whisper_language,
        max_seconds=settings.voice_max_seconds,
        download_root=str(settings.resolve(settings.models_path) / "whisper"),
    )
    app.state.wake_stt = WhisperSTT(
        settings.whisper_wake_model,
        "cpu",
        "int8",
        language="en",
        max_seconds=15,
        min_confidence=0.3,
        download_root=str(settings.resolve(settings.models_path) / "whisper"),
    )
    app.state.tts = PiperTTS(
        settings.resolve(settings.voices_path), default_voice=settings.tts_default_voice
    )
    # Load the speech models in the background, so the first voice message doesn't wait.
    # gather() schedules both right away and returns a future we can cancel at shutdown.
    warm_up = asyncio.gather(
        asyncio.to_thread(app.state.stt.warm_up),
        asyncio.to_thread(app.state.wake_stt.warm_up),
        asyncio.to_thread(app.state.tts.warm_up),
        return_exceptions=True,  # a missing voice model must not crash startup
    )

    log.info(
        "arthur_started",
        provider=llm.name,
        model=llm.model,
        fallback=settings.llm_fallback_provider,
        context_tokens=settings.llm_context_tokens,
        memories=await memory_manager.count(),
        documents=len(await documents.list_all()),
        tools=[t.name for t in app.state.tools.all()],
    )
    yield
    warm_up.cancel()
    await http_client.aclose()
    await embeddings.aclose()
    await llm.aclose()
    db.close()
    log.info("arthur_stopped")


def create_app() -> FastAPI:
    app = FastAPI(
        title="ARTHUR",
        description="Personal Multimodal AI Agent",
        version="0.2.0",
        lifespan=lifespan,
    )
    app.add_middleware(RequestContextMiddleware)
    register_exception_handlers(app)
    app.include_router(health.router)
    app.include_router(chat.router)
    app.include_router(memory.router)
    app.include_router(documents_routes.router)
    app.include_router(tools.router)
    app.include_router(voice.router)
    app.include_router(websocket.router)
    # Mounted last: API routes above win; everything else is served from frontend/.
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
    return app


app = create_app()
