"""Observability (Phase 20): the numbers are right, and nothing private becomes a label."""

import contextlib

import pytest

from app.llm.base import LLMUnavailableError
from app.llm.metered import MeteredProvider
from app.observability.metrics import Metrics, _quantile
from app.security.permissions import PermissionPolicy
from app.tools.calculator import CalculatorTool
from app.tools.registry import ToolRegistry
from tests.conftest import FakeLLM, tool_call

ORIGIN = {"Origin": "http://test"}


def value(metrics: Metrics, name: str, **labels: str) -> float:
    return metrics.registry.get_sample_value(name, labels) or 0.0


# ---------- percentiles from buckets ----------


def test_quantile_interpolates_inside_the_bucket():
    # 100 measurements: 50 took <= 1 s, 90 took <= 2 s, all took <= 5 s
    buckets = [(1.0, 50), (2.0, 90), (5.0, 100), (float("inf"), 100)]
    assert _quantile(buckets, 0.50) == 1000.0  # exactly at the first edge
    assert _quantile(buckets, 0.70) == 1500.0  # halfway through the 1-2 s bucket
    assert _quantile(buckets, 0.95) == 3500.0  # halfway through the 2-5 s bucket
    assert _quantile([], 0.5) is None
    assert _quantile([(1.0, 0), (float("inf"), 0)], 0.5) is None


def test_quantile_beyond_the_last_edge_reports_that_edge():
    buckets = [(1.0, 0), (float("inf"), 10)]  # everything was slower than 1 s
    assert _quantile(buckets, 0.95) == 1000.0  # "at least 1 s" - all the buckets can tell


# ---------- HTTP ----------


async def test_requests_are_counted_by_route_template(client):
    metrics: Metrics = client._transport.app.state.metrics

    await client.get("/health")
    await client.get("/health")
    await client.delete("/memories/abc-SECRET-id-123", headers=ORIGIN)  # 404
    await client.get("/no/such/page-with-PRIVATE-words")

    assert (
        value(metrics, "arthur_http_requests_total", method="GET", route="/health", status="200")
        == 2
    )
    assert (
        value(
            metrics,
            "arthur_http_requests_total",
            method="DELETE",
            route="/memories/{memory_id}",
            status="404",
        )
        == 1
    )
    assert value(metrics, "arthur_http_request_duration_seconds_count", route="/health") == 2

    await client.get("/")  # one of the page's own files
    assert value(metrics, "arthur_http_request_duration_seconds_count", route="static") == 1
    assert value(metrics, "arthur_http_request_duration_seconds_count", route="unmatched") == 1

    text = (await client.get("/metrics")).text
    assert "SECRET" not in text  # ids and typed paths never become labels
    assert "PRIVATE" not in text
    assert 'route="/memories/{memory_id}"' in text


async def test_metrics_endpoints(client, fake_llm):
    fake_llm.script = [[tool_call("calculator", expression="6 * 7")], "It is 42."]
    await client.post("/chat", json={"message": "what is 6 times 7?"}, headers=ORIGIN)

    exposition = await client.get("/metrics")
    assert exposition.headers["content-type"].startswith("text/plain")
    assert "# TYPE arthur_tool_calls_total counter" in exposition.text
    assert 'arthur_tool_calls_total{status="ok",tool="calculator"} 1.0' in exposition.text
    assert "what is 6 times 7" not in exposition.text  # no message text, ever

    summary = (await client.get("/metrics/summary")).json()
    assert summary["chat_turns"] == {"http:ok": 1}
    assert summary["tools"][0]["tool"] == "calculator"
    assert summary["tools"][0]["by_status"] == {"ok": 1}
    chat = next(r for r in summary["routes"] if r["route"] == "/chat")
    assert chat["requests"] == 1
    assert chat["p95_ms"] is not None
    assert summary["uptime_seconds"] >= 0
    # /metrics itself is not counted (it would only measure the measuring):
    assert all(not r["route"].startswith("/metrics") for r in summary["routes"])


async def test_crashes_are_counted_as_server_errors(client, fake_llm):
    """Found by the load test: a request that crashed (HTTP 500) was not counted at all."""
    metrics: Metrics = client._transport.app.state.metrics
    fake_llm.error = RuntimeError("boom")  # an unexpected, non-LLM error

    response = await client.post("/chat", json={"message": "Hello"}, headers=ORIGIN)

    assert response.status_code == 500
    assert (
        value(metrics, "arthur_http_requests_total", method="POST", route="/chat", status="500")
        == 1
    )
    chat = next(r for r in metrics.summary()["routes"] if r["route"] == "/chat")
    assert chat["errors"] == 1
    assert value(metrics, "arthur_chat_turns_total", channel="http", outcome="error") == 1


async def test_refused_requests_are_counted_by_reason(client):
    metrics: Metrics = client._transport.app.state.metrics

    await client.get("/health", headers={"Host": "evil.example"})
    await client.post("/chat", json={"message": "x"}, headers={"Origin": "https://evil.example"})

    assert value(metrics, "arthur_security_blocks_total", reason="wrong_host") == 1
    assert value(metrics, "arthur_security_blocks_total", reason="cross_origin") == 1
    assert (await client.get("/metrics/summary")).json()["security_blocks"] == {
        "wrong_host": 1,
        "cross_origin": 1,
    }


def test_open_tabs_gauge(ws_client):
    metrics: Metrics = ws_client.app.state.metrics
    with ws_client.websocket_connect("/ws") as ws:
        ws.receive_json()
        assert value(metrics, "arthur_ws_connections") == 1
    with ws_client.websocket_connect("/ws") as ws:  # reconnect: proves the first one was removed
        ws.receive_json()
        assert value(metrics, "arthur_ws_connections") == 1


# ---------- tools ----------


async def test_tool_calls_and_invented_tool_names():
    metrics = Metrics()
    registry = ToolRegistry(PermissionPolicy(), None, metrics=metrics)
    registry.register(CalculatorTool())

    await registry.execute("calculator", {"expression": "2 + 2"})
    await registry.execute("calculator", {"expression": "2 +"})  # an error
    await registry.execute("rm -rf / ; something the model invented", {})

    assert value(metrics, "arthur_tool_calls_total", tool="calculator", status="ok") == 1
    assert value(metrics, "arthur_tool_calls_total", tool="calculator", status="error") == 1
    # An invented name is counted as "unknown" - it never becomes a label of its own:
    assert value(metrics, "arthur_tool_calls_total", tool="unknown", status="error") == 1
    assert "invented" not in metrics.exposition().decode()
    assert value(metrics, "arthur_tool_duration_seconds_count", tool="calculator") == 2


# ---------- the language model ----------


async def test_model_calls_are_timed_and_counted():
    metrics = Metrics()
    llm = MeteredProvider(FakeLLM(script=["Hello there, how are you?"]), metrics)

    response = await llm.generate([])
    assert response.content == "Hello there, how are you?"
    model = llm.model

    assert (
        value(metrics, "arthur_llm_requests_total", model=model, kind="generate", outcome="ok") == 1
    )
    assert value(metrics, "arthur_llm_request_duration_seconds_count", model=model) == 1
    assert llm.name == llm.inner.name  # the wrapper looks like the provider it wraps


async def test_streamed_answers_record_first_token_and_chunks():
    metrics = Metrics()
    llm = MeteredProvider(FakeLLM(script=["one two three four"]), metrics)

    chunks = [event async for event in llm.stream_chat([])]

    assert len(chunks) >= 4
    model = llm.model
    assert value(metrics, "arthur_llm_first_token_seconds_count", model=model) == 1
    assert value(metrics, "arthur_llm_tokens_total", model=model, kind="completion") == len(chunks)
    assert (
        value(metrics, "arthur_llm_requests_total", model=model, kind="stream", outcome="ok") == 1
    )


async def test_stopped_and_failed_model_calls():
    metrics = Metrics()
    llm = MeteredProvider(
        FakeLLM(script=["one two three four", LLMUnavailableError("Ollama is not running")]),
        metrics,
    )
    model = llm.model

    stream = llm.stream_chat([])
    await anext(stream)
    await stream.aclose()  # the user pressed Stop after the first word

    with pytest.raises(LLMUnavailableError):
        await llm.generate([])

    assert (
        value(metrics, "arthur_llm_requests_total", model=model, kind="stream", outcome="stopped")
        == 1
    )
    assert (
        value(metrics, "arthur_llm_requests_total", model=model, kind="generate", outcome="error")
        == 1
    )
    summary = metrics.summary()["models"][0]
    assert summary["calls"] == 2
    assert summary["errors"] == 2  # anything that isn't "ok"


async def test_summary_of_an_idle_app_is_empty_not_broken():
    summary = Metrics().summary()
    assert summary["routes"] == [] and summary["tools"] == [] and summary["models"] == []
    assert summary["http_requests"] == 0
    with contextlib.suppress(KeyError):
        assert summary["chat_turns"] == {}


# ---------- the Grafana dashboard file (can't be opened here: Docker isn't installed) ----------


def test_grafana_dashboard_only_charts_metrics_that_exist():
    """Catches typos in the dashboard's queries even without running Grafana."""
    import json
    import re
    from pathlib import Path

    dashboard = json.loads(Path("deploy/grafana/dashboards/arthur.json").read_text("utf-8"))
    queried = set()
    for panel in dashboard["panels"]:
        for target in panel["targets"]:
            queried |= set(re.findall(r"arthur_[a-z_]+", target["expr"]))
    assert len(dashboard["panels"]) >= 10

    exported = set(re.findall(r"^# TYPE (arthur_[a-z_]+)", Metrics().exposition().decode(), re.M))
    # A histogram "x" is queried through its "x_bucket" series.
    known = exported | {f"{name}_bucket" for name in exported}
    assert queried <= known, f"dashboard uses unknown metrics: {queried - known}"
