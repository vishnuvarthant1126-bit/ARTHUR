"""Search providers.

- DuckDuckGoProvider: free, no account or API key (uses the `ddgs` library).
  Good for an MVP; heavy use can get temporarily rate-limited by DuckDuckGo.
- SearxngProvider: SearXNG is a free, open-source search engine you can run
  yourself (e.g. in Docker, Phase 23) - no limits, no tracking.
"""

import asyncio

import httpx

from app.search.base import SearchError, SearchProvider, SearchResult


class DuckDuckGoProvider(SearchProvider):
    name = "duckduckgo"

    def __init__(self, timeout_seconds: float = 10.0) -> None:
        self.timeout_seconds = timeout_seconds

    async def search(self, query: str, max_results: int) -> list[SearchResult]:
        # ddgs is synchronous: run it in a worker thread so the server stays responsive.
        try:
            raw = await asyncio.wait_for(
                asyncio.to_thread(self._search, query, max_results),
                timeout=self.timeout_seconds + 2,
            )
        except TimeoutError as exc:
            raise SearchError("The search engine didn't answer in time.", retryable=True) from exc
        except Exception as exc:  # ddgs raises its own exception types
            name = type(exc).__name__
            if "Ratelimit" in name:
                raise SearchError(
                    "The search engine is rate-limiting us. Try again in a minute.",
                    retryable=False,
                ) from exc
            raise SearchError(
                f"Web search failed ({name}). Are you online?", retryable=True
            ) from exc
        return [
            SearchResult(title=r.get("title", ""), url=r.get("href", ""), snippet=r.get("body", ""))
            for r in raw
            if r.get("href", "").startswith(("http://", "https://"))
        ]

    def _search(self, query: str, max_results: int) -> list[dict]:
        from ddgs import DDGS

        return DDGS(timeout=int(self.timeout_seconds)).text(query, max_results=max_results) or []


class SearxngProvider(SearchProvider):
    name = "searxng"

    def __init__(self, base_url: str, client: httpx.AsyncClient) -> None:
        self.base_url = base_url.rstrip("/")
        self.client = client

    async def search(self, query: str, max_results: int) -> list[SearchResult]:
        try:
            response = await self.client.get(
                f"{self.base_url}/search", params={"q": query, "format": "json"}, timeout=10.0
            )
        except httpx.HTTPError as exc:
            raise SearchError(
                f"SearXNG at {self.base_url} is unreachable.", retryable=True
            ) from exc
        if response.status_code != 200:
            raise SearchError(
                f"SearXNG returned HTTP {response.status_code}.",
                retryable=response.status_code in {429, 502, 503, 504},
            )
        results = [
            SearchResult(
                title=r.get("title", ""), url=r.get("url", ""), snippet=r.get("content", "")
            )
            for r in response.json().get("results", [])
            if r.get("url", "").startswith(("http://", "https://"))
        ]
        return results[:max_results]  # cut AFTER dropping non-web links, not before


def create_search_provider(
    name: str, *, searxng_url: str | None, client: httpx.AsyncClient
) -> SearchProvider:
    match name.lower():
        case "duckduckgo":
            return DuckDuckGoProvider()
        case "searxng":
            if not searxng_url:
                raise ValueError("SEARCH_PROVIDER=searxng needs SEARXNG_URL in .env")
            return SearxngProvider(searxng_url, client)
        case other:
            raise ValueError(f"Unknown SEARCH_PROVIDER '{other}'. Supported: duckduckgo, searxng")
