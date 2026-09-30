"""Tests for conversation memory and context-window trimming."""

from app.agent.orchestrator import Orchestrator
from app.llm.base import Role
from app.memory.short_term import Conversation, ConversationStore
from tests.conftest import FakeLLM


def test_add_exchange_stores_user_then_assistant():
    convo = Conversation(session_id="s1")
    convo.add_exchange("My name is Vishnu.", "Nice to meet you, Vishnu.")

    assert [(m.role, m.content) for m in convo.messages] == [
        (Role.USER, "My name is Vishnu."),
        (Role.ASSISTANT, "Nice to meet you, Vishnu."),
    ]


def test_recent_keeps_newest_messages_within_budget():
    convo = Conversation(session_id="s1")
    for i in range(10):
        convo.add_exchange(f"question {i} " + "x" * 400, f"answer {i} " + "y" * 400)

    # each message ~ 105 tokens; a 450-token budget fits the 4 newest
    recent = convo.recent(token_budget=450, max_messages=100)

    assert len(recent) == 4
    assert recent[0].content.startswith("question 8")
    assert recent[-1].content.startswith("answer 9")


def test_recent_never_starts_with_assistant_message():
    convo = Conversation(session_id="s1")
    convo.add_exchange("short", "a much longer answer " * 20)
    convo.add_exchange("hi", "hello")

    # Budget fits "hello", "hi" and part of the way into the long answer - not the pair.
    recent = convo.recent(token_budget=30, max_messages=100)

    assert recent[0].role == Role.USER
    assert [m.content for m in recent] == ["hi", "hello"]


def test_recent_respects_max_messages():
    convo = Conversation(session_id="s1")
    for i in range(5):
        convo.add_exchange(f"q{i}", f"a{i}")

    assert [m.content for m in convo.recent(10_000, max_messages=4)] == ["q3", "a3", "q4", "a4"]


def test_stored_messages_are_capped():
    convo = Conversation(session_id="s1", max_stored_messages=6)
    for i in range(10):
        convo.add_exchange(f"q{i}", f"a{i}")

    assert len(convo.messages) == 6
    assert convo.messages[0].content == "q7"


def test_store_evicts_least_recently_used_session():
    store = ConversationStore(max_sessions=2)
    store.get("a")
    store.get("b")
    store.get("a")  # "a" is now more recent than "b"
    store.get("c")

    assert "a" in store
    assert "b" not in store
    assert "c" in store


def test_store_forgets_idle_sessions():
    store = ConversationStore(ttl_seconds=60)
    store.get("old").last_active -= 120  # pretend it was idle for 2 minutes
    store.get("new")

    assert "old" not in store
    assert "new" in store


def test_orchestrator_includes_history_and_trims_to_context_window():
    from app.agent.prompts import SYSTEM_PROMPT
    from app.utils.tokens import estimate_tokens

    llm = FakeLLM()
    store = ConversationStore()
    # Room for the (growing) system prompt + reply reserve + ~1000 tokens of history.
    window = estimate_tokens(SYSTEM_PROMPT) + 500 + 1000
    orchestrator = Orchestrator(llm, store, context_tokens=window, reply_reserve_tokens=500)
    convo = store.get("s1")
    for i in range(20):
        convo.add_exchange(f"q{i} " + "x" * 200, f"a{i} " + "y" * 200)

    messages = orchestrator.build_messages(convo, "latest question")

    assert messages[0].role == Role.SYSTEM
    assert messages[-1].content == "latest question"
    history = messages[1:-1]
    assert 0 < len(history) < 40  # trimmed
    assert history[-1].content.startswith("a19")  # newest kept
    assert history[0].role == Role.USER


async def test_orchestrator_respond_saves_exchange():
    llm = FakeLLM(reply="Nice to meet you, Vishnu.")
    orchestrator = Orchestrator(llm, ConversationStore())

    await orchestrator.respond("s1", "My name is Vishnu.")
    await orchestrator.respond("s1", "What is my name?")

    second = llm.calls[-1]
    assert [m.content for m in second[1:]] == [
        "My name is Vishnu.",
        "Nice to meet you, Vishnu.",
        "What is my name?",
    ]
