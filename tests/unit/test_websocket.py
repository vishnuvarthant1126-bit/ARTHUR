"""Tests for the /ws streaming chat protocol."""

import pytest
from starlette.websockets import WebSocketDisconnect

from app.llm.base import LLMUnavailableError


def receive_until_done(ws) -> list[dict]:
    events = []
    while True:
        event = ws.receive_json()
        events.append(event)
        if event["type"] in ("done", "error"):
            return events


def test_chat_streams_tokens_then_done(ws_client):
    with ws_client.websocket_connect("/ws") as ws:
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


def test_multiple_messages_on_one_connection(ws_client, fake_llm):
    with ws_client.websocket_connect("/ws") as ws:
        for text in ("first", "second"):
            ws.send_json({"type": "chat", "message": text})
            assert receive_until_done(ws)[-1]["type"] == "done"

    assert [call[-1].content for call in fake_llm.calls] == ["first", "second"]


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
    with ws_client.websocket_connect("/ws") as ws:
        ws.send_text(frame)
        error = ws.receive_json()
        assert error["type"] == "error"
        assert error["error_type"] == "invalid_message"

        ws.send_json({"type": "ping"})  # connection still usable
        assert ws.receive_json() == {"type": "pong"}


def test_llm_failure_is_reported_as_error_event(ws_client, fake_llm):
    fake_llm.error = LLMUnavailableError("Cannot reach Ollama")

    with ws_client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "chat", "message": "Hello"})
        events = receive_until_done(ws)

    assert events[-1] == {
        "type": "error",
        "error_type": "llm_unavailable",
        "message": "Cannot reach Ollama",
    }


def test_stop_cancels_answer_in_progress(ws_client, fake_llm):
    fake_llm.reply = " ".join(["word"] * 200)
    fake_llm.token_delay = 0.02  # 200 tokens would take ~4s

    with ws_client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "chat", "message": "Tell me a long story"})
        assert ws.receive_json()["type"] == "status"
        ws.receive_json()  # at least one token has started flowing
        ws.send_json({"type": "stop"})
        events = receive_until_done(ws)

    done = events[-1]
    assert done["type"] == "done"
    assert done["stopped"] is True
    assert sum(e["type"] == "token" for e in events) < 199


def test_second_chat_while_busy_is_rejected(ws_client, fake_llm):
    fake_llm.reply = " ".join(["word"] * 50)
    fake_llm.token_delay = 0.02

    with ws_client.websocket_connect("/ws") as ws:
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
        ws.send_json({"type": "ping"})
        assert ws.receive_json() == {"type": "pong"}


def test_frontend_is_served(ws_client):
    response = ws_client.get("/")
    assert response.status_code == 200
    assert "<title>ARTHUR</title>" in response.text
    assert ws_client.get("/app.js").status_code == 200
