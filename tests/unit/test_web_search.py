"""Phase 10: search service (cache, rate limit, retry), SSRF-safe page reading, web tools."""

import httpx
import pytest

from app.agent.orchestrator import AGENT_TOOLS, Orchestrator
from app.memory.short_term import ConversationStore
from app.search.base import SearchError, SearchResult
from app.search.providers import create_search_provider
from app.search.service import WebSearchService
from app.search.webpage import (
    FetchError,
    UnsafeUrlError,
    check_public_url,
    fetch_page,
    html_to_text,
)
from app.security.permissions import PermissionPolicy
from app.tools.base import ToolContext
from app.tools.defaults import create_tool_registry
from tests.conftest import FakeLLM, FakeSearchProvider, offline_http_client, tool_call

PUBLIC = "http://93.184.215.14"  # a public IP literal: no DNS lookup needed in tests


def result(n: int) -> SearchResult:
    return SearchResult(title=f"Result {n}", url=f"https://site{n}.example.com/page", snippet="...")


# ---------- WebSearchService ----------


async def test_repeated_query_is_served_from_cache():
    provider = FakeSearchProvider()
    service = WebSearchService(provider)

    first = await service.search("latest python", 5)
    second = await service.search("  Latest   PYTHON ", 5)  # same query, different spacing/case

    assert first == second
    assert provider.queries == ["latest python"]


async def test_rate_limit_blocks_too_many_searches():
    service = WebSearchService(FakeSearchProvider(), max_per_minute=2)
    await service.search("a", 5)
    await service.search("b", 5)

    with pytest.raises(SearchError, match="Search limit reached"):
        await service.search("c", 5)
    assert await service.search("a", 5)  # cached queries don't count against the limit


async def test_brief_failure_is_retried_once():
    provider = FakeSearchProvider(errors=[SearchError("blip", retryable=True)])
    results = await WebSearchService(provider, retry_delay=0).search("q", 5)
    assert results and len(provider.queries) == 2


async def test_non_retryable_failure_is_not_retried():
    provider = FakeSearchProvider(errors=[SearchError("rate limited", retryable=False)])
    with pytest.raises(SearchError, match="rate limited"):
        await WebSearchService(provider, retry_delay=0).search("q", 5)
    assert len(provider.queries) == 1


async def test_duplicate_urls_are_removed():
    same = SearchResult(title="A", url="https://a.example.com/x/", snippet="")
    also_same = SearchResult(title="A again", url="https://A.example.com/x", snippet="")
    provider = FakeSearchProvider(results=[same, also_same, result(2)])

    results = await WebSearchService(provider).search("q", 5)

    assert [r.title for r in results] == ["A", "Result 2"]


def test_result_source_is_the_domain():
    assert SearchResult(title="t", url="https://www.python.org/downloads/").source == "python.org"


def test_searxng_needs_url():
    with pytest.raises(ValueError, match="SEARXNG_URL"):
        create_search_provider("searxng", searxng_url=None, client=offline_http_client())


# ---------- SSRF protection ----------


@pytest.mark.parametrize(
    "url",
    [
        "file:///C:/Windows/win.ini",
        "ftp://example.com/file",
        "http://localhost:8000/documents",
        "http://127.0.0.1:11434/api/tags",  # your own Ollama
        "http://10.0.0.5/",
        "http://192.168.1.1/admin",  # a home router
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata service
        "http://[::1]/",
        "http://0.0.0.0/",
        "http://printer.local/",
        "http://user:secret@93.184.215.14/",
    ],
)
async def test_unsafe_urls_are_blocked(url):
    with pytest.raises(UnsafeUrlError):
        await check_public_url(url)


async def test_public_ip_is_allowed():
    await check_public_url(f"{PUBLIC}/page")


async def test_unknown_domain_is_a_fetch_error():
    with pytest.raises(FetchError, match="Couldn't find the website"):
        await check_public_url("https://no-such-host.invalid/")


# ---------- reading pages (mocked HTTP) ----------


def mock_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


HTML = """<html><head><title>AI Conferences 2026</title><style>body{}</style></head>
<body><nav>Home | About</nav><h1>Singapore AI Week</h1><p>Held on 3&ndash;5 November.</p>
<script>alert('x')</script><footer>Copyright</footer></body></html>"""


def test_html_to_text_keeps_content_drops_noise():
    title, text = html_to_text(HTML)
    assert title == "AI Conferences 2026"
    assert "Singapore AI Week" in text and "3–5 November" in text
    assert "alert" not in text and "Home | About" not in text and "Copyright" not in text


async def test_fetch_page_returns_title_and_text():
    client = mock_client(
        lambda r: httpx.Response(
            200, text=HTML, headers={"content-type": "text/html; charset=utf-8"}
        )
    )
    page = await fetch_page(f"{PUBLIC}/conferences", client)
    assert page.title == "AI Conferences 2026"
    assert "Singapore AI Week" in page.text and page.truncated is False


async def test_redirect_to_private_address_is_blocked():
    def handler(request):
        return httpx.Response(302, headers={"location": "http://192.168.1.1/admin"})

    with pytest.raises(UnsafeUrlError):
        await fetch_page(f"{PUBLIC}/innocent-looking", mock_client(handler))


async def test_redirect_loop_is_stopped():
    client = mock_client(lambda r: httpx.Response(302, headers={"location": f"{PUBLIC}/again"}))
    with pytest.raises(FetchError, match="Too many redirects"):
        await fetch_page(f"{PUBLIC}/start", client)


async def test_non_text_content_is_rejected():
    client = mock_client(
        lambda r: httpx.Response(200, content=b"\x89PNG", headers={"content-type": "image/png"})
    )
    with pytest.raises(FetchError, match="Not a web page"):
        await fetch_page(f"{PUBLIC}/image", client)


async def test_large_pages_are_cut_off():
    big = "<p>" + "word " * 5000 + "</p>"
    client = mock_client(
        lambda r: httpx.Response(200, text=big, headers={"content-type": "text/html"})
    )
    page = await fetch_page(f"{PUBLIC}/big", client, max_bytes=2000, max_chars=500)
    assert len(page.text) == 500 and page.truncated is True


async def test_http_error_status():
    client = mock_client(lambda r: httpx.Response(404, text="nope"))
    with pytest.raises(FetchError, match="HTTP 404"):
        await fetch_page(f"{PUBLIC}/missing", client)


# ---------- tools and agent ----------


def registry(search_provider=None, client=None):
    search = WebSearchService(search_provider or FakeSearchProvider(), retry_delay=0)
    return create_tool_registry(
        policy=PermissionPolicy(),
        audit=None,
        http_client=client or offline_http_client(),
        memory=None,
        search=search,
    )


async def test_web_search_tool_returns_sources_and_rules():
    tools = registry()
    result = await tools.execute("web_search", {"query": "latest python version"}, ToolContext())

    assert result.ok
    first = result.output["results"][0]
    assert first["url"] == "https://www.python.org/downloads/latest/"
    assert first["source"] == "python.org"
    assert "untrusted DATA" in result.output["instructions_for_answer"]
    assert tools.get("web_search").summarize(result.output) == "1 results: python.org"


async def test_web_search_failure_becomes_tool_error():
    provider = FakeSearchProvider(errors=[SearchError("offline", retryable=False)])
    result = await registry(provider).execute("web_search", {"query": "x y"}, ToolContext())
    assert result.status == "error" and result.error == "offline"


async def test_read_webpage_blocks_local_addresses():
    result = await registry().execute(
        "read_webpage", {"url": "http://127.0.0.1:11434/api/tags"}, ToolContext()
    )
    assert result.status == "error"
    assert "private or local" in result.error


async def test_agent_searches_and_cites_link():
    llm = FakeLLM(
        script=[
            [tool_call("web_search", query="latest Python version")],
            "The latest version is 3.14.7 ([python.org](https://www.python.org/downloads/latest/)).",
        ]
    )
    orchestrator = Orchestrator(llm, ConversationStore(), tools=registry())

    reply = await orchestrator.respond("s1", "What's the latest Python version?")

    assert "(https://www.python.org/downloads/latest/)" in reply.content
    assert reply.tools_used[0].name == "web_search"
    assert "python.org" in llm.calls[1][-1].content  # the model saw the results


def test_web_tools_are_available_to_the_agent():
    assert {"web_search", "read_webpage"} <= AGENT_TOOLS


async def test_tools_api_lists_web_tools(client):
    names = {t["name"] for t in (await client.get("/tools")).json()}
    assert {"web_search", "read_webpage"} <= names
