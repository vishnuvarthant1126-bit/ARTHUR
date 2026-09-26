"""Tests for the /ws streaming chat protocol."""

from contextlib import contextmanager

import pytest
from starlette.websockets import WebSocketDisconnect

from app.llm.base import LLMUnavailableError, Role


def receive_until_done(ws) -> list[dict]:
    events = []
    while True:
        event = ws.receive_json()
        events.append(event)
        if event["type"] in ("done", "error"):
            return events


@contextmanager
def session(ws_client, session_id: str | None = None):
    """Connect, consume the initial `session` event, yield (websocket, that event)."""
    url = f"/ws?session_id={session_id}" if session_id else "/ws"
    with ws_client.websocket_connect(url) as ws:
        event = ws.receive_json()
        assert event["type"] == "session"
        yield ws, event


def history_of(ws_client, session_id: str) -> list[dict]:
    with session(ws_client, session_id) as (_, event):
        return event["history"]


def test_connect_announces_new_session(ws_client):
    with session(ws_client) as (_, event):
        assert len(event["session_id"]) == 32
        assert event["history"] == []


def test_invalid_session_id_is_replaced(ws_client):
    with session(ws_client, "bad id!") as (_, event):
        assert event["session_id"] != "bad id!"


def test_chat_streams_tokens_then_done(ws_client):
    with session(ws_client) as (ws, _):
        ws.send_json({"type": "chat", "message": "Hello Arthur"})
        events = receive_until_done(ws)

    assert events[0]["type"] == "status"
    assert events[0]["state"] == "thinking"
    tokens = [e["content"] for e in events if e["type"] == "token"]
    assert "".join(tokens) == "Hello. How can I help?"
    done = events[-1]
    assert done["type"] == "done"
    assert done["stopped"] is False
    assert done["model"] == "fake-model"
    assert done["request_id"] == events[0]["request_id"]


def test_conversation_is_remembered_between_messages(ws_client, fake_llm):
    with session(ws_client) as (ws, _):
        for text in ("My name is Vishnu.", "What is my name?"):
            ws.send_json({"type": "chat", "message": text})
            assert receive_until_done(ws)[-1]["type"] == "done"

    second_call = fake_llm.calls[-1]
    assert [m.role for m in second_call] == [Role.SYSTEM, Role.USER, Role.ASSISTANT, Role.USER]
    assert second_call[1].content == "My name is Vishnu."


def test_reconnect_with_session_id_restores_history(ws_client):
    with session(ws_client) as (ws, event):
        ws.send_json({"type": "chat", "message": "My name is Vishnu."})
        receive_until_done(ws)

    assert history_of(ws_client, event["session_id"]) == [
        {"role": "user", "content": "My name is Vishnu."},
        {"role": "assistant", "content": "Hello. How can I help?"},
    ]


def test_clear_forgets_conversation(ws_client, fake_llm):
    with session(ws_client) as (ws, _):
        ws.send_json({"type": "chat", "message": "My name is Vishnu."})
        receive_until_done(ws)
        ws.send_json({"type": "clear"})
        assert ws.receive_json() == {"type": "cleared"}
        ws.send_json({"type": "chat", "message": "What is my name?"})
        receive_until_done(ws)

    assert len(fake_llm.calls[-1]) == 2  # system + user: nothing remembered


@pytest.mark.parametrize(
    "frame",
    [
        "not json",
        '{"type": "chat", "message": "   "}',
        '{"type": "launch_missiles"}',
        '{"message": "no type"}',
    ],
)
def test_invalid_frames_return_error_and_keep_connection(ws_client, frame):
    with session(ws_client) as (ws, _):
        ws.send_text(frame)
        error = ws.receive_json()
        assert error["type"] == "error"
        assert error["error_type"] == "invalid_message"

        ws.send_json({"type": "ping"})  # connection still usable
        assert ws.receive_json() == {"type": "pong"}


def test_llm_failure_is_reported_and_not_remembered(ws_client, fake_llm):
    fake_llm.error = LLMUnavailableError("Cannot reach Ollama")

    with session(ws_client) as (ws, event):
        ws.send_json({"type": "chat", "message": "Hello"})
        events = receive_until_done(ws)

    assert events[-1] == {
        "type": "error",
        "error_type": "llm_unavailable",
        "message": "Cannot reach Ollama",
    }
    assert history_of(ws_client, event["session_id"]) == []


def test_stop_cancels_answer_and_keeps_partial_reply(ws_client, fake_llm):
    fake_llm.reply = " ".join(["word"] * 200)
    fake_llm.token_delay = 0.02  # 200 tokens would take ~4s

    with session(ws_client) as (ws, event):
        ws.send_json({"type": "chat", "message": "Tell me a long story"})
        assert ws.receive_json()["type"] == "status"
        ws.receive_json()  # at least one token has started flowing
        ws.send_json({"type": "stop"})
        events = receive_until_done(ws)

    done = events[-1]
    assert done["type"] == "done"
    assert done["stopped"] is True
    assert sum(e["type"] == "token" for e in events) < 199

    saved_reply = history_of(ws_client, event["session_id"])[1]["content"]
    assert saved_reply.startswith("word")
    assert len(saved_reply) < len(fake_llm.reply)


def test_second_chat_while_busy_is_rejected(ws_client, fake_llm):
    fake_llm.reply = " ".join(["word"] * 50)
    fake_llm.token_delay = 0.02

    with session(ws_client) as (ws, _):
        ws.send_json({"type": "chat", "message": "first"})
        ws.send_json({"type": "chat", "message": "second"})
        events = receive_until_done(ws)
        while events[-1]["type"] != "error" or events[-1]["error_type"] != "busy":
            events = receive_until_done(ws)
        ws.send_json({"type": "stop"})

    assert events[-1]["error_type"] == "busy"


def test_foreign_origin_is_rejected(ws_client):
    with (
        pytest.raises(WebSocketDisconnect) as exc_info,
        ws_client.websocket_connect("/ws", headers={"origin": "https://evil.example"}),
    ):
        pass
    assert exc_info.value.code == 1008


def test_same_origin_is_accepted(ws_client):
    headers = {"origin": "http://testserver"}
    with ws_client.websocket_connect("/ws", headers=headers) as ws:
        assert ws.receive_json()["type"] == "session"
        ws.send_json({"type": "ping"})
        assert ws.receive_json() == {"type": "pong"}


def test_frontend_is_served(ws_client):
    response = ws_client.get("/")
    assert response.status_code == 200
    assert "<title>ARTHUR</title>" in response.text
    assert ws_client.get("/app.js").status_code == 200
