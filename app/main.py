"""ARTHUR API entry point.

Run with:  uvicorn app.main:app --reload
"""

import asyncio
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.agent.attachments import AttachmentStore
from app.agent.executor import AgentLimits, PlanLimits
from app.agent.orchestrator import Orchestrator
from app.api import websocket
from app.api.errors import register_exception_handlers
from app.api.middleware import RequestContextMiddleware
from app.api.routes import (
    attachments,
    chat,
    health,
    memory,
    reminders,
    system,
    tools,
    vision,
    voice,
)
from app.api.routes import documents as documents_routes
from app.api.routes import metrics as metrics_routes
from app.browser.agent import BrowserAgent
from app.computer.apps import allowed_apps
from app.computer.desktop import DesktopController, workspace_rules
from app.config.settings import Settings, get_settings
from app.database.database import Database
from app.files.workspace import Workspace
from app.llm.base import LLMProvider
from app.llm.factory import create_llm_provider
from app.llm.metered import MeteredProvider
from app.memory.long_term import MemoryRepository
from app.memory.manager import MemoryManager
from app.memory.short_term import ConversationStore
from app.memory.vector_store import ChromaVectorStore
from app.observability.logging import configure_logging, get_logger
from app.observability.metrics import Metrics
from app.rag.documents import DocumentService
from app.rag.embeddings import create_embedding_provider
from app.rag.retrieval import DocumentRetriever
from app.scheduler.reminders import ReminderService
from app.scheduler.runner import NotificationHub, ReminderScheduler
from app.search.providers import create_search_provider
from app.search.service import WebSearchService
from app.search.webpage import new_page_client
from app.security.audit import AuditLog
from app.security.permissions import PermissionPolicy
from app.security.rate_limit import RateLimiter
from app.tools.defaults import create_tool_registry
from app.tools.registry import ToolRegistry
from app.vision.provider import OllamaVision, VisionProvider
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
    metrics: Metrics | None = None,
    vision: VisionProvider | None = None,
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
        metrics=metrics,
        vision=vision,
        summary_delay_seconds=settings.history_summary_delay_seconds,
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Code before `yield` runs at startup; code after it runs at shutdown."""
    settings = get_settings()
    configure_logging(settings.log_level, json_logs=settings.json_logs)

    db = Database(settings.resolve(settings.database_path))
    db.create_tables()

    metrics: Metrics = app.state.metrics
    llm = MeteredProvider(create_llm_provider(settings), metrics)  # times every model call
    embeddings = create_embedding_provider(
        settings.embedding_provider,
        settings.ollama_base_url,
        settings.embedding_model,
        keep_alive=settings.ollama_keep_alive,
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
    page_client = new_page_client()
    search = WebSearchService(
        create_search_provider(
            settings.search_provider, searxng_url=settings.searxng_url, client=http_client
        ),
        max_per_minute=settings.search_rate_limit_per_minute,
        cache_seconds=settings.search_cache_minutes * 60,
    )

    # Starts lazily (first browser_open), so it costs nothing until used.
    browser = (
        BrowserAgent(
            headless=settings.browser_headless,
            on_block=metrics.security_blocks.labels("egress").inc,
        )
        if settings.browser_enabled
        else None
    )
    workspace = Workspace(settings.file_roots, settings.files_save_dir)
    desktop = build_desktop(settings, workspace)
    vision = (
        OllamaVision(
            settings.ollama_base_url, settings.vision_model, keep_alive=settings.vision_keep_alive
        )
        if settings.vision_enabled
        else None
    )

    app.state.llm = llm
    app.state.memory = memory_manager
    app.state.documents = documents
    app.state.retriever = retriever
    app.state.audit = audit
    # Reminders (Phase 18): saved in SQLite, delivered to open tabs by the scheduler loop.
    reminder_service = ReminderService(db)
    hub = NotificationHub()
    scheduler = ReminderScheduler(
        reminder_service,
        hub,
        interval_seconds=settings.reminder_check_seconds,
        metrics=metrics,
    )
    app.state.reminders = reminder_service
    app.state.hub = hub
    app.state.scheduler = scheduler
    app.state.vision = vision
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
        page_client=page_client,
        workspace=workspace,
        browser=browser,
        desktop=desktop,
        vision=vision,
        reminders=reminder_service,
        on_reminder_change=scheduler.poke,
        metrics=metrics,
        default_timeout_seconds=settings.tools_default_timeout_seconds,
        memory_min_score=settings.memory_min_score,
        document_min_score=settings.rag_min_score,
    )
    app.state.orchestrator = build_orchestrator(
        llm, settings, memory_manager, app.state.tools, retriever, metrics, vision
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
    speech_models = (app.state.stt, app.state.wake_stt, app.state.tts)
    warm_up = asyncio.gather(
        *(asyncio.to_thread(m.warm_up) for m in speech_models if settings.voice_warm_up),
        return_exceptions=True,  # a missing voice model must not crash startup
    )
    # The same for the language models: load them and cache the standing prompt now, so the
    # first message doesn't wait ~11 s (see Orchestrator.warm_up).
    model_warm_up = (
        asyncio.create_task(app.state.orchestrator.warm_up(), name="arthur-model-warm-up")
        if settings.llm_warm_up
        else None
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
    scheduler.start()
    yield
    await scheduler.stop()
    app.state.orchestrator.stop_background()
    warm_up.cancel()
    if model_warm_up is not None:
        model_warm_up.cancel()
    if browser is not None:
        await browser.close()
    if desktop is not None:
        desktop.close()
    if vision is not None:
        await vision.aclose()
    await http_client.aclose()
    await page_client.aclose()
    await embeddings.aclose()
    await llm.aclose()
    db.close()
    log.info("arthur_stopped")


def build_desktop(settings: Settings, workspace: Workspace) -> DesktopController | None:
    """Computer use (Phase 16): Windows only, and only the apps listed in .env."""
    apps = allowed_apps(settings.computer_allowed_apps)
    if sys.platform != "win32" or not settings.computer_use_enabled or not apps:
        return None
    folder_allowed, resolve_folder = workspace_rules(workspace)
    default_folder = workspace.roots[0] if workspace.roots else workspace.save_dir
    return DesktopController(apps, folder_allowed, resolve_folder, default_folder)


def create_app() -> FastAPI:
    app = FastAPI(
        title="ARTHUR",
        description="Personal Multimodal AI Agent",
        version="0.2.0",
        lifespan=lifespan,
    )
    settings = get_settings()
    # Front-door checks (see app/api/middleware.py); set here so they also apply in tests.
    app.state.allowed_hosts = settings.extra_hosts
    app.state.metrics = Metrics()
    app.state.attachments = AttachmentStore()  # files attached to chat messages (RAM)
    app.state.rate_limiter = RateLimiter(
        {"chat": settings.rate_limit_chat_per_minute}, enabled=settings.rate_limit_enabled
    )
    app.add_middleware(RequestContextMiddleware)
    register_exception_handlers(app)
    app.include_router(health.router)
    app.include_router(system.router)
    app.include_router(chat.router)
    app.include_router(memory.router)
    app.include_router(documents_routes.router)
    app.include_router(tools.router)
    app.include_router(voice.router)
    app.include_router(vision.router)
    app.include_router(attachments.router)
    app.include_router(reminders.router)
    app.include_router(metrics_routes.router)
    app.include_router(websocket.router)
    # Mounted last: API routes above win; everything else is served from frontend/.
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
    return app


app = create_app()
