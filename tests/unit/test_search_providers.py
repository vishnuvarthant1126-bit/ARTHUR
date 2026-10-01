"""The real search clients (DuckDuckGo, SearXNG), tested without the internet.

Coverage showed these at 45 %: everything else used FakeSearchProvider, so the code
that actually talks to a search engine had never run in a test.
"""

import time

import httpx
import pytest

from app.search.base import SearchError
from app.search.providers import DuckDuckGoProvider, SearxngProvider, create_search_provider

# ---------- SearXNG ----------


def searxng(handler) -> SearxngProvider:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return SearxngProvider("http://searx.test/", client)


async def test_searxng_maps_results_and_drops_non_web_links():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(
            200,
            json={
                "results": [
                    {"title": "Python", "url": "https://www.python.org/", "content": "Welcome"},
                    {"title": "Trap", "url": "javascript:alert(1)", "content": "x"},
                    {"title": "File", "url": "file:///C:/secret.txt"},
                    {"title": "Docs", "url": "http://docs.python.org/"},
                    {"title": "Third", "url": "https://pypi.org/"},
                ]
            },
        )

    results = await searxng(handler).search("python", max_results=2)

    assert seen["url"] == "http://searx.test/search?q=python&format=json"
    assert [r.url for r in results] == ["https://www.python.org/", "http://docs.python.org/"]
    assert results[0].title == "Python" and results[0].snippet == "Welcome"
    assert results[1].snippet == ""  # a missing field is not an error
    # Two junk links came before the second real one: they must not use up the limit,
    # and the third real result is cut off by max_results=2.


@pytest.mark.parametrize(("status", "retryable"), [(503, True), (429, True), (404, False)])
async def test_searxng_errors_say_whether_retrying_helps(status, retryable):
    provider = searxng(lambda request: httpx.Response(status))
    with pytest.raises(SearchError) as error:
        await provider.search("python", 5)
    assert error.value.retryable is retryable
    assert str(status) in str(error.value)


async def test_searxng_unreachable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(SearchError, match="unreachable") as error:
        await searxng(handler).search("python", 5)
    assert error.value.retryable


# ---------- DuckDuckGo (the ddgs library is replaced by a stand-in) ----------


def duckduckgo(result=None, error: Exception | None = None, delay: float = 0.0):
    provider = DuckDuckGoProvider(timeout_seconds=0.1)

    def fake_search(query: str, max_results: int) -> list[dict]:
        time.sleep(delay)
        if error:
            raise error
        return result or []

    provider._search = fake_search
    return provider


async def test_duckduckgo_maps_results_and_drops_non_web_links():
    raw = [
        {"title": "Python", "href": "https://www.python.org/", "body": "Welcome"},
        {"title": "No link"},
        {"title": "Trap", "href": "javascript:alert(1)", "body": "x"},
    ]
    results = await duckduckgo(raw).search("python", 5)
    assert len(results) == 1
    assert (results[0].title, results[0].url, results[0].snippet) == (
        "Python",
        "https://www.python.org/",
        "Welcome",
    )


async def test_duckduckgo_rate_limit_is_not_retried():
    class RatelimitException(Exception):  # the name ddgs uses
        pass

    with pytest.raises(SearchError, match="rate-limiting") as error:
        await duckduckgo(error=RatelimitException("202")).search("python", 5)
    assert not error.value.retryable  # retrying at once would only make it worse


async def test_duckduckgo_other_failures_can_be_retried():
    with pytest.raises(SearchError, match="Are you online") as error:
        await duckduckgo(error=ConnectionError("no network")).search("python", 5)
    assert error.value.retryable


async def test_duckduckgo_timeout():
    with pytest.raises(SearchError, match="in time") as error:
        await duckduckgo(delay=3.0).search("python", 5)
    assert error.value.retryable


# ---------- choosing the provider ----------


def test_create_search_provider():
    client = httpx.AsyncClient()
    assert isinstance(
        create_search_provider("DuckDuckGo", searxng_url=None, client=client), DuckDuckGoProvider
    )
    searx = create_search_provider("searxng", searxng_url="http://localhost:8888", client=client)
    assert isinstance(searx, SearxngProvider) and searx.base_url == "http://localhost:8888"
    with pytest.raises(ValueError, match="needs SEARXNG_URL"):
        create_search_provider("searxng", searxng_url=None, client=client)
    with pytest.raises(ValueError, match="Unknown SEARCH_PROVIDER"):
        create_search_provider("google", searxng_url=None, client=client)
