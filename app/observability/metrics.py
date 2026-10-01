"""ARTHUR's metrics: numbers over time (how many, how fast, how often it fails).

Three kinds:
    Counter    only goes up            "chat turns so far"
    Gauge      goes up and down        "open browser tabs"
    Histogram  measurements in buckets "how many answers took <= 1 s, <= 2 s, ..."
               -> percentiles: p95 = the time 95 % of requests were faster than

Prometheus "pulls": it fetches GET /metrics (plain text) every few seconds and stores
the history; Grafana draws charts from that. ARTHUR also has its own small dashboard
(/dashboard.html), fed by `summary()`.

Label rule: labels come from small fixed sets (tool names, route templates, status
codes) - NEVER from user text, or the number of series would grow without limit
("cardinality explosion").
"""

import time
from collections.abc import Iterable

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest

# Bucket edges in seconds. Web requests are fast; the language model is slow.
HTTP_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60)
LLM_BUCKETS = (0.25, 0.5, 1, 2, 3, 5, 8, 13, 21, 34, 60, 120)
TOOL_BUCKETS = (0.005, 0.025, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 200)


class Metrics:
    """One instance per app (its own registry, so tests never share counters)."""

    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        self.started = time.time()
        r = self.registry

        self.http_requests = Counter(
            "arthur_http_requests_total", "HTTP requests", ["method", "route", "status"], registry=r
        )
        self.http_duration = Histogram(
            "arthur_http_request_duration_seconds",
            "Time to answer an HTTP request",
            ["route"],
            buckets=HTTP_BUCKETS,
            registry=r,
        )
        self.chat_turns = Counter(
            "arthur_chat_turns_total",
            "Chat messages answered (outcome: ok, error, stopped)",
            ["channel", "outcome"],
            registry=r,
        )
        self.ws_connections = Gauge(
            "arthur_ws_connections", "Open browser tabs (WebSocket connections)", registry=r
        )

        self.llm_requests = Counter(
            "arthur_llm_requests_total",
            "Calls to the language model",
            ["model", "kind", "outcome"],
            registry=r,
        )
        self.llm_duration = Histogram(
            "arthur_llm_request_duration_seconds",
            "Time for one complete model call",
            ["model"],
            buckets=LLM_BUCKETS,
            registry=r,
        )
        self.llm_first_token = Histogram(
            "arthur_llm_first_token_seconds",
            "Streaming: time until the first piece of the answer",
            ["model"],
            buckets=LLM_BUCKETS,
            registry=r,
        )
        self.llm_tokens = Counter(
            "arthur_llm_tokens_total",
            "Tokens (kind: prompt, completion; streamed answers are counted in chunks)",
            ["model", "kind"],
            registry=r,
        )

        self.tool_calls = Counter(
            "arthur_tool_calls_total",
            "Tool calls (status: ok, error, needs_confirmation, denied)",
            ["tool", "status"],
            registry=r,
        )
        self.tool_duration = Histogram(
            "arthur_tool_duration_seconds",
            "Time a tool ran",
            ["tool"],
            buckets=TOOL_BUCKETS,
            registry=r,
        )

        self.security_blocks = Counter(
            "arthur_security_blocks_total",
            "Requests refused by a safety rule",
            ["reason"],  # wrong_host, cross_origin, rate_limited, egress
            registry=r,
        )
        self.reminders_delivered = Counter(
            "arthur_reminders_delivered_total", "Reminders delivered", ["late"], registry=r
        )
        self.uptime = Gauge("arthur_uptime_seconds", "Seconds since ARTHUR started", registry=r)
        self.uptime.set_function(lambda: time.time() - self.started)

    # ---------- output ----------

    def exposition(self) -> bytes:
        """The Prometheus text format served at GET /metrics."""
        return generate_latest(self.registry)

    def summary(self) -> dict:
        """The same numbers, arranged for ARTHUR's own dashboard page."""
        samples = _Samples(self.registry.collect())
        routes = []
        for route in samples.label_values("arthur_http_request_duration_seconds_count", "route"):
            count = samples.total("arthur_http_request_duration_seconds_count", route=route)
            errors = sum(
                value
                for labels, value in samples.items("arthur_http_requests_total")
                if labels["route"] == route and labels["status"].startswith("5")
            )
            routes.append(
                {
                    "route": route,
                    "requests": int(count),
                    "errors": int(errors),
                    **samples.latency("arthur_http_request_duration_seconds", route=route),
                }
            )
        tools = []
        for tool in samples.label_values("arthur_tool_calls_total", "tool"):
            by_status = {
                labels["status"]: int(value)
                for labels, value in samples.items("arthur_tool_calls_total")
                if labels["tool"] == tool
            }
            tools.append(
                {
                    "tool": tool,
                    "calls": sum(by_status.values()),
                    "by_status": by_status,
                    **samples.latency("arthur_tool_duration_seconds", tool=tool),
                }
            )
        models = []
        for model in samples.label_values("arthur_llm_request_duration_seconds_count", "model"):
            models.append(
                {
                    "model": model,
                    "calls": int(
                        samples.total("arthur_llm_request_duration_seconds_count", model=model)
                    ),
                    "errors": int(
                        sum(
                            value
                            for labels, value in samples.items("arthur_llm_requests_total")
                            if labels["model"] == model and labels["outcome"] != "ok"
                        )
                    ),
                    "prompt_tokens": int(
                        samples.total("arthur_llm_tokens_total", model=model, kind="prompt")
                    ),
                    "completion_tokens": int(
                        samples.total("arthur_llm_tokens_total", model=model, kind="completion")
                    ),
                    **samples.latency("arthur_llm_request_duration_seconds", model=model),
                    "first_token": samples.latency("arthur_llm_first_token_seconds", model=model),
                }
            )
        return {
            "uptime_seconds": round(time.time() - self.started),
            "open_tabs": int(samples.total("arthur_ws_connections")),
            "chat_turns": {
                f"{labels['channel']}:{labels['outcome']}": int(value)
                for labels, value in samples.items("arthur_chat_turns_total")
            },
            "http_requests": int(samples.total("arthur_http_requests_total")),
            "routes": sorted(routes, key=lambda r: -r["requests"]),
            "tools": sorted(tools, key=lambda t: -t["calls"]),
            "models": models,
            "security_blocks": {
                labels["reason"]: int(value)
                for labels, value in samples.items("arthur_security_blocks_total")
            },
            "reminders_delivered": int(samples.total("arthur_reminders_delivered_total")),
        }


class _Samples:
    """Read values back out of the registry (name -> [(labels, value)])."""

    def __init__(self, families: Iterable) -> None:
        self._by_name: dict[str, list[tuple[dict, float]]] = {}
        for family in families:
            for sample in family.samples:
                self._by_name.setdefault(sample.name, []).append((sample.labels, sample.value))

    def items(self, name: str) -> list[tuple[dict, float]]:
        return self._by_name.get(name, [])

    def total(self, name: str, **labels: str) -> float:
        return sum(
            value
            for sample_labels, value in self.items(name)
            if all(sample_labels.get(k) == v for k, v in labels.items())
        )

    def label_values(self, name: str, label: str) -> list[str]:
        return sorted({labels[label] for labels, _ in self.items(name) if label in labels})

    def latency(self, histogram: str, **labels: str) -> dict:
        """Average, p50 and p95 in milliseconds, estimated from the histogram's buckets."""
        count = self.total(f"{histogram}_count", **labels)
        if not count:
            return {"avg_ms": None, "p50_ms": None, "p95_ms": None}
        buckets = sorted(
            (float(sample_labels["le"]), value)
            for sample_labels, value in self.items(f"{histogram}_bucket")
            if all(sample_labels.get(k) == v for k, v in labels.items())
        )
        return {
            "avg_ms": round(self.total(f"{histogram}_sum", **labels) / count * 1000, 1),
            "p50_ms": _quantile(buckets, 0.50),
            "p95_ms": _quantile(buckets, 0.95),
        }


def _quantile(buckets: list[tuple[float, float]], q: float) -> float | None:
    """Estimate a percentile the way Prometheus does: find the bucket that contains the
    q-th measurement and interpolate inside it. It is an estimate - only as precise as
    the bucket edges."""
    total = buckets[-1][1] if buckets else 0
    if not total:
        return None
    rank = q * total
    lower_edge, lower_count = 0.0, 0.0
    for upper_edge, cumulative in buckets:
        if cumulative >= rank:
            if upper_edge == float("inf"):
                return round(lower_edge * 1000, 1)  # beyond the last edge: all we know
            inside = cumulative - lower_count
            fraction = (rank - lower_count) / inside if inside else 1.0
            return round((lower_edge + (upper_edge - lower_edge) * fraction) * 1000, 1)
        lower_edge, lower_count = upper_edge, cumulative
    return None
