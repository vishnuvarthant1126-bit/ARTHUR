"""Tests for POST /chat and GET /health, using the FakeLLM."""

import pytest

from app.llm.base import LLMResponseError, LLMTimeoutError, LLMUnavailableError, Role


async def test_chat_returns_llm_reply(client, fake_llm):
    response = await client.post("/chat", json={"message": "Hello Arthur"})

    assert response.status_code == 200
    body = response.json()
    assert body["response"] == "Hello. How can I help?"
    assert body["model"] == "fake-model"
    assert len(body["session_id"]) == 32


async def test_chat_remembers_conversation_with_session_id(client, fake_llm):
    first = await client.post("/chat", json={"message": "My name is Vishnu."})
    session_id = first.json()["session_id"]

    await client.post("/chat", json={"message": "What is my name?", "session_id": session_id})

    sent = fake_llm.calls[-1]
    assert [m.role for m in sent] == [Role.SYSTEM, Role.USER, Role.ASSISTANT, Role.USER]
    assert sent[1].content == "My name is Vishnu."
    assert sent[-1].content == "What is my name?"


async def test_chat_without_session_id_starts_fresh(client, fake_llm):
    await client.post("/chat", json={"message": "My name is Vishnu."})
    await client.post("/chat", json={"message": "What is my name?"})

    assert len(fake_llm.calls[-1]) == 2  # system + user only


async def test_delete_chat_forgets_conversation(client, fake_llm):
    first = await client.post("/chat", json={"message": "My name is Vishnu."})
    session_id = first.json()["session_id"]

    deleted = await client.delete(f"/chat/{session_id}")
    await client.post("/chat", json={"message": "What is my name?", "session_id": session_id})

    assert deleted.status_code == 204
    assert len(fake_llm.calls[-1]) == 2


async def test_chat_rejects_malformed_session_id(client):
    response = await client.post("/chat", json={"message": "hi", "session_id": "../../etc"})
    assert response.status_code == 422


async def test_chat_sends_system_prompt_then_user_message(client, fake_llm):
    await client.post("/chat", json={"message": "  Hello Arthur  "})

    sent = fake_llm.calls[0]
    assert [m.role for m in sent] == [Role.SYSTEM, Role.USER]
    assert "ARTHUR" in sent[0].content
    assert sent[1].content == "Hello Arthur"  # whitespace stripped


@pytest.mark.parametrize("payload", [{}, {"message": ""}, {"message": "   "}, {"msg": "hi"}])
async def test_chat_rejects_invalid_input(client, payload):
    response = await client.post("/chat", json=payload)
    assert response.status_code == 422


async def test_chat_rejects_overlong_message(client):
    response = await client.post("/chat", json={"message": "x" * 8001})
    assert response.status_code == 422


@pytest.mark.parametrize(
    ("error", "status", "error_type"),
    [
        (LLMUnavailableError("Ollama down"), 503, "llm_unavailable"),
        (LLMTimeoutError("slow"), 504, "llm_timeout"),
        (LLMResponseError("bad"), 502, "llm_bad_response"),
    ],
)
async def test_chat_maps_llm_errors_to_http_errors(client, fake_llm, error, status, error_type):
    fake_llm.error = error

    response = await client.post("/chat", json={"message": "Hello"})

    assert response.status_code == status
    body = response.json()
    assert body["error"]["type"] == error_type
    assert body["request_id"] == response.headers["X-Request-ID"]


async def test_unexpected_error_returns_generic_500(client, fake_llm):
    fake_llm.error = RuntimeError("secret internal detail")  # type: ignore[assignment]

    response = await client.post("/chat", json={"message": "Hello"})

    assert response.status_code == 500
    assert response.json()["error"]["type"] == "internal_error"
    assert "secret" not in response.text


async def test_request_id_is_generated_or_echoed(client):
    generated = await client.post("/chat", json={"message": "hi"})
    assert len(generated.headers["X-Request-ID"]) == 12

    echoed = await client.post("/chat", json={"message": "hi"}, headers={"X-Request-ID": "abc-123"})
    assert echoed.headers["X-Request-ID"] == "abc-123"

    unsafe = await client.post("/chat", json={"message": "hi"}, headers={"X-Request-ID": "a b\n"})
    assert unsafe.headers["X-Request-ID"] != "a b\n"


async def test_health_reports_llm(client):
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "llm": {"provider": "fake", "model": "fake-model", "reachable": True},
    }
