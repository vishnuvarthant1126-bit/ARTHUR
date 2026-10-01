"""Metrics endpoints (Phase 20).

    GET /metrics          Prometheus text format - what Prometheus scrapes
    GET /metrics/summary  the same numbers as JSON - feeds /dashboard.html

Both contain only counts and timings (tool names, route names, status codes) - never
message text, arguments or file names.
"""

from fastapi import APIRouter, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST

router = APIRouter(tags=["metrics"])


@router.get("/metrics")
async def prometheus_metrics(request: Request) -> Response:
    return Response(request.app.state.metrics.exposition(), media_type=CONTENT_TYPE_LATEST)


@router.get("/metrics/summary")
async def metrics_summary(request: Request) -> dict:
    return request.app.state.metrics.summary()
