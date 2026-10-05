"""Phase 25: GET /status reports every part of ARTHUR separately."""

import asyncio

from app.api.routes.system import Component, _check
from app.llm.base import LLMUnavailableError
from app.security.rate_limit import group_for


def by_name(body: dict) -> dict[str, dict]:
    return {c["name"]: c for c in body["components"]}


async def test_status_lists_every_part(client):
    response = await client.get("/status")
    assert response.status_code == 200
    body = response.json()
    parts = by_name(body)
    assert list(parts) == [
        "Language model", "Memory", "Documents", "Reminders", "Voice", "Vision", "Browser",
        "Desktop apps",
    ]  # fmt: skip
    assert body["overall"] == "ok"
    assert body["uptime_seconds"] >= 0
    assert parts["Language model"]["state"] == "ok"
    assert parts["Memory"]["detail"] == "0 facts"
    assert parts["Vision"]["state"] == "ok"  # the test app has a FakeVision


async def test_counts_follow_what_is_stored(client):
    await client.post("/reminders", json={"text": "stretch", "when": "in 2 hours"})
    parts = by_name((await client.get("/status")).json())
    assert parts["Reminders"]["detail"] == "1 upcoming"


async def test_a_switched_off_part_is_off_not_a_problem(client):
    app = client._transport.app
    app.state.vision = None
    body = (await client.get("/status")).json()
    assert by_name(body)["Vision"] == {"name": "Vision", "state": "off", "detail": "switched off"}
    assert body["overall"] == "ok"


async def test_a_broken_part_is_reported_and_the_rest_still_answers(client, fake_llm):
    app = client._transport.app

    async def broken_count() -> int:
        raise RuntimeError("database is locked")

    app.state.memory.count = broken_count
    fake_llm.error = LLMUnavailableError("Ollama is not running")
    body = (await client.get("/status")).json()
    parts = by_name(body)
    assert body["overall"] == "problem"
    assert parts["Memory"]["state"] == "problem"
    assert "database" not in parts["Memory"]["detail"]  # no internals on the page
    assert parts["Documents"]["state"] == "ok"


async def test_a_hanging_check_times_out(monkeypatch):
    monkeypatch.setattr("app.api.routes.system.CHECK_TIMEOUT_SECONDS", 0.01)

    async def hangs() -> Component:
        await asyncio.sleep(5)
        raise AssertionError("never")

    result = await _check("Slow part", hangs)
    assert result.state == "problem" and result.detail == "no answer in time"


def test_status_counts_as_a_read_request():
    assert group_for("GET", "/status") == "read"


def test_the_page_has_the_five_status_lamps_and_no_inline_code():
    from pathlib import Path

    page = (Path(__file__).resolve().parents[2] / "frontend" / "index.html").read_text("utf-8")
    for lamp in ("online", "listening", "thinking", "executing", "speaking"):
        assert f'data-lamp="{lamp}"' in page
    # The Content-Security-Policy forbids inline scripts and styles - they would not run.
    assert "<script>" not in page and "style=" not in page and "onclick" not in page
