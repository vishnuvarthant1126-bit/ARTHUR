"""WebSearchService: a polite, reliable wrapper around any search provider.

    query ─► clean up ─► cache hit? ─► return instantly (no network)
                      └► rate limit ok? ─► provider (1 retry on a brief failure)
                                        ─► de-duplicate by URL ─► cache ─► return

- Cache: the same query within `cache_seconds` reuses the earlier results.
- Rate limit: at most `max_per_minute` real searches, so the engine doesn't block us.
- Retry: one retry after a short pause for errors marked retryable - but only when the
  failed attempt was quick (a slow failure, e.g. offline, would just double the wait).
"""

import asyncio
import time
from collections import OrderedDict, deque

from app.observability.logging import get_logger
from app.search.base import SearchError, SearchProvider, SearchResult

log = get_logger(__name__)


class WebSearchService:
    def __init__(
        self,
        provider: SearchProvider,
        *,
        max_per_minute: int = 10,
        cache_seconds: float = 600.0,
        cache_size: int = 200,
        retry_delay: float = 1.0,
        quick_failure_seconds: float = 3.0,
    ) -> None:
        self.provider = provider
        self.quick_failure_seconds = quick_failure_seconds
        self.max_per_minute = max_per_minute
        self.cache_seconds = cache_seconds
        self.cache_size = cache_size
        self.retry_delay = retry_delay
        self._cache: OrderedDict[tuple[str, int], tuple[float, list[SearchResult]]] = OrderedDict()
        self._recent: deque[float] = deque()  # timestamps of real searches
        self._lock = asyncio.Lock()

    async def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        query = " ".join(query.split())
        key = (query.lower(), max_results)

        cached = self._cache.get(key)
        if cached and time.monotonic() - cached[0] < self.cache_seconds:
            self._cache.move_to_end(key)
            log.info("web_search", cached=True, results=len(cached[1]))
            return cached[1]

        async with self._lock:
            self._check_rate_limit()
            self._recent.append(time.monotonic())

        start = time.perf_counter()
        try:
            results = await self.provider.search(query, max_results)
        except SearchError as exc:
            # A retry helps after a quick hiccup. After a SLOW failure (offline: ~9 s of
            # refused connections) it only doubles the wait - measured 22 s (Phase 28).
            if not exc.retryable or time.perf_counter() - start > self.quick_failure_seconds:
                raise
            log.warning("web_search_retry", error=str(exc))
            await asyncio.sleep(self.retry_delay)
            results = await self.provider.search(query, max_results)

        results = _dedupe(results)[:max_results]
        self._cache[key] = (time.monotonic(), results)
        while len(self._cache) > self.cache_size:
            self._cache.popitem(last=False)
        log.info(
            "web_search",
            cached=False,
            provider=self.provider.name,
            results=len(results),
            duration_ms=round((time.perf_counter() - start) * 1000, 1),
        )
        return results

    def _check_rate_limit(self) -> None:
        now = time.monotonic()
        while self._recent and now - self._recent[0] > 60:
            self._recent.popleft()
        if len(self._recent) >= self.max_per_minute:
            wait = int(60 - (now - self._recent[0])) + 1
            raise SearchError(
                f"Search limit reached ({self.max_per_minute}/min). Try again in {wait}s."
            )


def _dedupe(results: list[SearchResult]) -> list[SearchResult]:
    seen: set[str] = set()
    unique = []
    for r in results:
        key = r.url.rstrip("/").lower()
        if key not in seen:
            seen.add(key)
            unique.append(r)
    return unique
