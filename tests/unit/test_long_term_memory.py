"""Phase 5: long-term memory - storage, search, policy and the chat flow."""

import httpx
import pytest

from app.agent.orchestrator import Orchestrator
from app.llm.base import LLMUnavailableError, Role
from app.memory import policy
from app.memory.manager import MemoryManager
from app.memory.short_term import ConversationStore
from app.memory.vector_store import InMemoryVectorStore, cosine
from app.rag.embeddings import OllamaEmbeddings
from tests.conftest import FakeLLM, make_memory

# ---------- MemoryManager ----------


async def test_save_retrieve_and_list(memory: MemoryManager):
    saved, created = await memory.save_memory(
        "The user's main project is called ARTHUR.", "project"
    )

    assert created is True
    assert (await memory.retrieve_memory(saved.id)).content == saved.content
    assert [m.content for m in await memory.list_memories()] == [saved.content]
    assert await memory.count() == 1


async def test_search_returns_most_similar_first(memory: MemoryManager):
    # FakeEmbeddings only matches shared words; true "by meaning" search is
    # covered by the live test with the real embedding model.
    await memory.save_memory("The user's favourite language is Python.", "preference")
    await memory.save_memory("The user's main project is called ARTHUR.", "project")

    results = await memory.search_memory("What is my project called?", k=5, min_score=0.3)

    assert results[0].memory.content == "The user's main project is called ARTHUR."
    assert all(r.score >= 0.3 for r in results)


async def test_search_ignores_unrelated_memories(memory: MemoryManager):
    await memory.save_memory("The user's favourite language is Python.")

    assert await memory.search_memory("weather forecast tomorrow", min_score=0.3) == []


async def test_near_duplicate_updates_instead_of_duplicating(memory: MemoryManager):
    first, _ = await memory.save_memory("The user's favourite language is Python.")
    second, created = await memory.save_memory("The user's favourite language is Python!")

    assert created is False
    assert second.id == first.id
    assert await memory.count() == 1


async def test_delete_removes_from_both_stores(memory: MemoryManager):
    saved, _ = await memory.save_memory("The user likes tea.")

    assert await memory.delete_memory(saved.id) is True
    assert await memory.retrieve_memory(saved.id) is None
    assert memory.vectors.count() == 0
    assert await memory.delete_memory(saved.id) is False


async def test_failed_vector_write_rolls_back_database_row():
    manager, _ = make_memory()

    class BrokenStore(InMemoryVectorStore):
        def upsert(self, *args):
            raise RuntimeError("disk full")

    manager.vectors = BrokenStore()
    with pytest.raises(RuntimeError):
        await manager.save_memory("The user likes tea.")
    assert await manager.count() == 0  # no orphan row left behind


@pytest.mark.parametrize("content", ["", "   ", "x" * 1001])
async def test_invalid_memory_content_is_rejected(memory: MemoryManager, content):
    with pytest.raises(ValueError):
        await memory.save_memory(content)


def test_cosine_similarity():
    assert cosine([1, 0], [1, 0]) == 1.0
    assert cosine([1, 0], [0, 1]) == 0.0


async def test_ollama_embeddings_use_task_prefixes():
    sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = httpx.Request("POST", "/", content=request.content).read()
        sent.append(__import__("json").loads(body)["input"])
        return httpx.Response(200, json={"embeddings": [[0.1, 0.2]] * len(sent[-1])})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://o")
    embeddings = OllamaEmbeddings("http://o", "nomic-embed-text", client=client)

    await embeddings.embed_documents(["fact"])
    await embeddings.embed_query("question")

    assert sent == [["search_document: fact"], ["search_query: question"]]


# ---------- memory policy ----------


@pytest.mark.parametrize(
    ("text", "intent"),
    [
        ("Remember that my main project is called ARTHUR.", "remember"),
        ("Arthur, please remember my favourite colour is blue", "remember"),
        ("Don't forget that I have a meeting on Monday", "remember"),
        ("Note that I prefer short answers", "remember"),
        ("Note down that the wifi password changes monthly", "remember"),
        ("Note: my train leaves at 7", "remember"),
        ("Make a note that I like tea", "remember"),
        ("Note 3 about the garden project: the north bed needs compost.", "none"),
        ("Notes from today's meeting are attached", "none"),
        ("Do you remember my name?", "none"),
        ("Remember", "none"),
        ("I remember my first computer fondly", "none"),
        ("Forget that my favourite colour is blue", "forget"),
        ("Delete the memory about my colour", "forget"),
        ("Delete this file", "none"),
        ("What is 2 + 2", "none"),
    ],
)
def test_detect_intent(text, intent):
    assert policy.detect_intent(text) == intent


@pytest.mark.parametrize(
    "text",
    [
        "Remember my password is hunter2",
        "Remember my API key is sk-123",
        "Remember my card number 4111 1111 1111 1111",
        "Remember my bank account details",
    ],
)
def test_sensitive_data_is_detected(text):
    assert policy.contains_sensitive_data(text)


def test_normal_facts_are_not_sensitive():
    assert not policy.contains_sensitive_data("Remember that my main project is called ARTHUR")


# ---------- chat flow (orchestrator) ----------


def make_orchestrator(llm: FakeLLM) -> tuple[Orchestrator, MemoryManager]:
    memory, _ = make_memory()
    return Orchestrator(llm, ConversationStore(), memory=memory, memory_min_score=0.3), memory


async def test_remember_saves_extracted_fact_and_confirms():
    llm = FakeLLM(
        structured_reply='{"fact": "The user\'s main project is called ARTHUR.", '
        '"category": "project"}'
    )
    orchestrator, memory = make_orchestrator(llm)

    reply = await orchestrator.respond("s1", "Remember that my main project is called ARTHUR.")

    assert reply.content.startswith("Got it. I'll remember that")
    [saved] = await memory.list_memories()
    assert saved.content == "The user's main project is called ARTHUR."
    assert saved.category == "project"


async def test_remember_falls_back_to_users_words_if_extraction_fails():
    llm = FakeLLM(structured_reply="not json")
    orchestrator, memory = make_orchestrator(llm)

    await orchestrator.respond("s1", "Remember that I prefer short answers")

    [saved] = await memory.list_memories()
    assert saved.content == "I prefer short answers."


async def test_remember_refuses_secrets_without_calling_llm():
    llm = FakeLLM()
    orchestrator, memory = make_orchestrator(llm)

    reply = await orchestrator.respond("s1", "Remember my password is hunter2")

    assert "won't store" in reply.content
    assert await memory.count() == 0
    assert llm.calls == []  # the secret never even reached the model


async def test_relevant_memories_ride_with_the_latest_message():
    """Memories go in a <context> block in front of the newest message - NOT into the
    system prompt, which must stay identical between turns so the model can cache it."""
    from app.agent.prompts import SYSTEM_PROMPT

    llm = FakeLLM()
    orchestrator, memory = make_orchestrator(llm)
    await memory.save_memory("The user's main project is called ARTHUR.")
    await memory.save_memory("The user's favourite drink is tea.")

    await orchestrator.respond("s1", "What is my project called?")

    system, newest = llm.calls[-1][0], llm.calls[-1][-1]
    assert system.role == Role.SYSTEM
    assert system.content == SYSTEM_PROMPT  # byte-for-byte the same on every turn
    assert newest.role == Role.USER
    assert newest.content.startswith("<context>")
    assert "The user's main project is called ARTHUR." in newest.content
    assert "tea" not in newest.content  # unrelated memory not included
    assert newest.content.endswith("</context>\n\nWhat is my project called?")
    # The conversation keeps what the user really typed, without the block:
    assert orchestrator.history("s1")[0].content == "What is my project called?"


async def test_chat_still_works_when_embeddings_are_down():
    llm = FakeLLM()
    orchestrator, memory = make_orchestrator(llm)

    async def broken(_):
        raise LLMUnavailableError("embedding model down")

    memory.embeddings.embed_query = broken

    reply = await orchestrator.respond("s1", "Hello")

    assert reply.content == "Hello. How can I help?"


async def test_forget_asks_for_confirmation_then_deletes_on_yes():
    orchestrator, memory = make_orchestrator(FakeLLM())
    await memory.save_memory("The user's favourite colour is blue.")

    question = await orchestrator.respond("s1", "Forget that my favourite colour is blue")
    assert "Should I forget" in question.content
    assert await memory.count() == 1  # nothing deleted yet

    done = await orchestrator.respond("s1", "yes")
    assert "forgotten" in done.content
    assert await memory.count() == 0


async def test_forget_keeps_memory_on_no():
    orchestrator, memory = make_orchestrator(FakeLLM())
    await memory.save_memory("The user's favourite colour is blue.")

    await orchestrator.respond("s1", "Forget my favourite colour")
    reply = await orchestrator.respond("s1", "no")

    assert reply.content == "Okay, I'll keep it."
    assert await memory.count() == 1


async def test_pending_forget_is_cancelled_by_unrelated_message():
    llm = FakeLLM()
    orchestrator, memory = make_orchestrator(llm)
    await memory.save_memory("The user's favourite colour is blue.")

    await orchestrator.respond("s1", "Forget my favourite colour")
    await orchestrator.respond("s1", "Tell me a joke")  # moves on
    await orchestrator.respond("s1", "yes")  # too late - no longer confirms anything

    assert await memory.count() == 1


async def test_forget_with_no_match():
    orchestrator, _ = make_orchestrator(FakeLLM())

    reply = await orchestrator.respond("s1", "Forget my shoe size")

    assert "couldn't find" in reply.content


# ---------- API ----------


async def test_memory_api_crud(client):
    created = await client.post(
        "/memories", json={"content": "The user likes tea.", "category": "preference"}
    )
    assert created.status_code == 201
    memory_id = created.json()["id"]

    listed = (await client.get("/memories")).json()
    assert listed["count"] == 1

    found = (await client.get("/memories/search", params={"q": "tea"})).json()
    assert found[0]["memory"]["id"] == memory_id

    assert (await client.delete(f"/memories/{memory_id}")).status_code == 204
    assert (await client.delete(f"/memories/{memory_id}")).status_code == 404


async def test_memory_api_refuses_secrets(client):
    response = await client.post("/memories", json={"content": "My password is hunter2"})
    assert response.status_code == 422
