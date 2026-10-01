"""Generate deploy/grafana/dashboards/arthur.json (the Grafana dashboard for ARTHUR).

    python scripts/make_grafana_dashboard.py

Each panel is a PromQL query over the metrics defined in app/observability/metrics.py.
Two PromQL ideas are enough to read them:
    rate(counter[5m])                         how fast a counter grew, per second
    histogram_quantile(0.95, rate(..._bucket[5m]))   the p95 from a histogram's buckets
"""

import json
from pathlib import Path

TARGET = Path(__file__).resolve().parents[1] / "deploy" / "grafana" / "dashboards" / "arthur.json"
DATASOURCE = {"type": "prometheus", "uid": "arthur-prometheus"}


def per_minute(metric: str, by: str) -> str:
    return f"sum by ({by}) (rate({metric}[5m])) * 60"


def quantile(q: float, histogram: str, by: str = "") -> str:
    labels = f"le, {by}" if by else "le"
    return f"histogram_quantile({q}, sum by ({labels}) (rate({histogram}_bucket[5m])))"


LLM_TIME = "arthur_llm_request_duration_seconds"
HTTP_TIME = "arthur_http_request_duration_seconds"

# (title, unit, [(legend, PromQL)], panel type)
PANELS = [
    (
        "Messages answered / min",
        "short",
        [("{{channel}} {{outcome}}", per_minute("arthur_chat_turns_total", "channel, outcome"))],
        "timeseries",
    ),
    ("Open tabs", "short", [("tabs", "arthur_ws_connections")], "stat"),
    (
        "Model call time",
        "s",
        [("typical (p50)", quantile(0.5, LLM_TIME)), ("slow (p95)", quantile(0.95, LLM_TIME))],
        "timeseries",
    ),
    (
        "Time to first words (p50)",
        "s",
        [("first token", quantile(0.5, "arthur_llm_first_token_seconds"))],
        "timeseries",
    ),
    (
        "Model calls / min by outcome",
        "short",
        [("{{outcome}}", per_minute("arthur_llm_requests_total", "outcome"))],
        "timeseries",
    ),
    (
        "Tokens generated / min",
        "short",
        [("{{kind}}", per_minute("arthur_llm_tokens_total", "kind"))],
        "timeseries",
    ),
    (
        "Tool calls / min",
        "short",
        [("{{tool}}", per_minute("arthur_tool_calls_total", "tool"))],
        "timeseries",
    ),
    (
        "Tool time (p95)",
        "s",
        [("{{tool}}", quantile(0.95, "arthur_tool_duration_seconds", "tool"))],
        "timeseries",
    ),
    (
        "HTTP requests / s by address",
        "reqps",
        [("{{route}}", "sum by (route) (rate(arthur_http_requests_total[5m]))")],
        "timeseries",
    ),
    (
        "HTTP time (p95) by address",
        "s",
        [("{{route}}", quantile(0.95, HTTP_TIME, "route"))],
        "timeseries",
    ),
    (
        "Refused by safety rules (total)",
        "short",
        [("{{reason}}", "sum by (reason) (arthur_security_blocks_total)")],
        "timeseries",
    ),
    (
        "Reminders delivered (total)",
        "short",
        [("late={{late}}", "sum by (late) (arthur_reminders_delivered_total)")],
        "stat",
    ),
    ("Uptime", "s", [("uptime", "arthur_uptime_seconds")], "stat"),
]


def build() -> dict:
    panels = []
    for index, (title, unit, queries, kind) in enumerate(PANELS):
        panels.append(
            {
                "id": index + 1,
                "title": title,
                "type": kind,
                "datasource": DATASOURCE,
                "gridPos": {"h": 8, "w": 12, "x": 12 * (index % 2), "y": 8 * (index // 2)},
                "fieldConfig": {"defaults": {"unit": unit}, "overrides": []},
                "targets": [
                    {
                        "refId": chr(65 + i),
                        "expr": expr,
                        "legendFormat": legend,
                        "datasource": DATASOURCE,
                    }
                    for i, (legend, expr) in enumerate(queries)
                ],
            }
        )
    return {
        "uid": "arthur-overview",
        "title": "ARTHUR",
        "tags": ["arthur"],
        "timezone": "browser",
        "schemaVersion": 39,
        "version": 1,
        "refresh": "10s",
        "time": {"from": "now-30m", "to": "now"},
        "panels": panels,
    }


if __name__ == "__main__":
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(json.dumps(build(), indent=2) + "\n", encoding="utf-8")
    print(f"wrote {TARGET} ({len(PANELS)} panels)")
