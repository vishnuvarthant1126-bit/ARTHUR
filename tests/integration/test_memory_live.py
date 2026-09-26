"""Live long-term memory test: real embedding model + real ChromaDB (in a temp folder)."""

import pytest

from app.config.settings import get_settings
from app.database.database import Database
from app.llm.factory import create_llm_provider
from app.main import build_orchestrator
from app.memory.long_term import MemoryRepository
from app.memory.manager import MemoryManager
from app.memory.vector_store import ChromaVectorStore
from app.rag.embeddings import create_embedding_provider

pytestmark = pytest.mark.integration


@pytest.fixture
async def live_memory(tmp_path):
    settings = get_settings()
    embeddings = create_embedding_provider(
        settings.embedding_provider, settings.ollama_base_url, settings.embedding_model
    )
    try:
        await embeddings.embed_query("ping")
    except Exception:
        await embeddings.aclose()
        pytest.skip("Ollama embedding model is not available")
    db = Database(tmp_path / "test.db")
    db.create_tables()
    manager = MemoryManager(
        MemoryRepository(db), ChromaVectorStore(tmp_path / "chroma"), embeddings
    )
    yield manager
    await embeddings.aclose()
    db.close()


async def test_semantic_search_finds_by_meaning(live_memory: MemoryManager):
    settings = get_settings()
    await live_memory.save_memory("The user's main project is called ARTHUR.", "project")
    await live_memory.save_memory("The user's favourite programming language is Python.")
    await live_memory.save_memory("The user is allergic to peanuts.", "personal")

    # No shared keywords with the stored sentence - only the meaning matches.
    results = await live_memory.search_memory(
        "Which software am I building?", k=3, min_score=settings.memory_min_score
    )
    assert results, "expected at least one relevant memory"
    assert results[0].memory.content == "The user's main project is called ARTHUR."

    unrelated = await live_memory.search_memory(
        "What's the capital of France?", k=3, min_score=settings.memory_min_score
    )
    assert unrelated == []


async def test_remember_then_recall_in_new_conversation(live_memory: MemoryManager):
    settings = get_settings()
    llm = create_llm_provider(settings)
    try:
        orchestrator = build_orchestrator(llm, settings, live_memory)

        confirm = await orchestrator.respond(
            "session-a", "Remember that my main project is called ARTHUR."
        )
        assert "remember" in confirm.content.lower()

        # A brand-new conversation: short-term memory is empty, long-term is not.
        answer = await orchestrator.respond("session-b", "What is my main project called?")
        assert "arthur" in answer.content.lower()
    finally:
        await llm.aclose()
