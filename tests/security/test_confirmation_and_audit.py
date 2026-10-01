"""Confirmations can't be given by accident, and secrets don't reach the audit log."""

import time

import pytest

from app.agent.orchestrator import PENDING_MAX_AGE_SECONDS, Orchestrator
from app.memory.policy import is_no, is_yes
from app.memory.short_term import ConversationStore
from app.security.audit import AuditLog, redact, scrub_text
from app.security.permissions import PermissionPolicy
from app.tools.defaults import create_tool_registry
from tests.conftest import FakeLLM, make_memory, offline_http_client, tool_call

# ---------- "yes" must be a pure, short confirmation ----------


@pytest.mark.parametrize(
    "text",
    ["yes", "Yes.", "y", "yeah", "yep", "ok", "okay", "sure", "confirm", "go ahead", "do it",
     "yes please", "Yes, go ahead.", "ok do it", "yes, do it now", "Okay, thanks"],
)  # fmt: skip
def test_real_confirmations(text):
    assert is_yes(text)


@pytest.mark.parametrize(
    "text",
    [
        "ok, what is this?",  # used to count as yes (it starts with "ok")
        "yes but not the second one",
        "yes, delete everything",
        "ok wait",
        "sure, after you show me the list",
        "okay so what happens if I say yes",
        "yesterday I asked about this",
        "do it later",
        "y not",
        "",
        "no",
    ],
)
def test_anything_else_is_not_a_yes(text):
    assert not is_yes(text)


def test_no_still_works_loosely():
    assert is_no("no")
    assert is_no("No, keep it.")
    assert is_no("cancel that please")


async def scenario():
    memory, db = make_memory()
    saved, _ = await memory.save_memory("The user likes tea.")
    llm = FakeLLM(script=[[tool_call("delete_memory", memory_id=saved.id)], "Deleting a memory."])
    tools = create_tool_registry(
        policy=PermissionPolicy(),
        audit=AuditLog(db),
        http_client=offline_http_client(),
        memory=memory,
    )
    orchestrator = Orchestrator(llm, ConversationStore(), memory=memory, tools=tools)
    await orchestrator.respond("s1", "delete my tea memory")
    assert orchestrator.conversations.get("s1").pending is not None
    return orchestrator, memory


async def test_a_question_that_starts_with_ok_does_not_confirm():
    orchestrator, memory = await scenario()

    await orchestrator.respond("s1", "ok, what is this?")

    assert await memory.count() == 1  # nothing was deleted
    assert orchestrator.conversations.get("s1").pending is None  # and the request is dropped


async def test_a_late_yes_is_refused():
    orchestrator, memory = await scenario()
    pending = orchestrator.conversations.get("s1").pending
    pending.asked_at = time.monotonic() - PENDING_MAX_AGE_SECONDS - 1  # asked 5+ minutes ago

    reply = await orchestrator.respond("s1", "yes")

    assert "expired" in reply.content
    assert await memory.count() == 1


async def test_a_prompt_yes_still_works():
    orchestrator, memory = await scenario()
    reply = await orchestrator.respond("s1", "Yes, go ahead.")
    assert reply.content.startswith("Done")
    assert await memory.count() == 0


# ---------- the audit log never stores secrets ----------


@pytest.mark.parametrize(
    ("text", "kept", "gone"),
    [
        ("my password is hunter2", "my password is ***", "hunter2"),
        ("PIN: 4821 for the door", "PIN: ***", "4821"),
        ("api key = sk-abc123def456ghi789", "api key = ***", "sk-abc123"),
        ("card 4111 1111 1111 1111 exp 12/29", "card ****", "4111"),
        ("use sk-live_51Hxxxxxxxxxxxxxxxx here", "use ***", "51Hxxxx"),
        ("Shopping list: milk, eggs", "Shopping list: milk, eggs", "\x00"),
    ],
)
def test_scrub_text(text, kept, gone):
    scrubbed = scrub_text(text)
    assert kept in scrubbed
    assert gone not in scrubbed


def test_redact_handles_keys_and_values_at_any_depth():
    arguments = {
        "app": "notepad",
        "text": "note: password is hunter2",
        "api_key": "abc",
        "nested": [{"token": "xyz", "note": "card 4111-1111-1111-1111"}],
        "shortcut": "ctrl+z",
    }
    result = redact(arguments)
    assert result["app"] == "notepad"
    assert result["shortcut"] == "ctrl+z"  # which key was pressed stays visible
    assert result["api_key"] == "***"
    assert result["nested"][0]["token"] == "***"
    assert "hunter2" not in str(result)
    assert "4111" not in str(result)


# ---------- accepted risk: Chroma's server advisories ----------


def test_chroma_is_only_used_embedded():
    """pip-audit lists advisories for Chroma's HTTP SERVER (CVE-2026-45829 and others, no
    fix released yet). ARTHUR uses Chroma inside its own process and starts no server, so
    they don't apply - this test fails if that ever changes."""
    from pathlib import Path

    source = "".join(p.read_text(encoding="utf-8") for p in Path("app").rglob("*.py"))
    assert "PersistentClient" in source
    assert "chromadb.HttpClient" not in source
    assert "chroma run" not in source
