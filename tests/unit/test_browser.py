"""Browser agent (Phase 15): risk levels (no browser needed) + real Chromium tests.

The Chromium tests serve fixture pages from a tiny local HTTP server. A fake URL checker
allows ONLY that server - every other host counts as "private" and must be blocked.
They are skipped if Chromium isn't installed (python -m playwright install chromium).
"""

import http.server
import threading
from pathlib import Path

import pytest

from app.browser.agent import BrowserAgent, BrowserError
from app.browser.risk import click_level, type_level
from app.search.webpage import UnsafeUrlError
from app.security.permissions import PermissionPolicy
from app.tools.base import PermissionLevel, ToolContext
from app.tools.browser_tools import (
    BrowserClickTool,
    BrowserFindTextTool,
    BrowserOpenTool,
    BrowserTypeTool,
)
from app.tools.registry import ToolRegistry

# ---------- risk rules (pure functions) ----------


@pytest.mark.parametrize(
    ("element", "level"),
    [
        ({"kind": "link", "text": "Downloads for Windows"}, PermissionLevel.LOW_RISK),
        ({"kind": "button", "text": "Download"}, PermissionLevel.CONFIRM),
        ({"kind": "link", "text": "Donate"}, PermissionLevel.CONFIRM),
        ({"kind": "link", "text": "About us"}, PermissionLevel.LOW_RISK),
        ({"kind": "link", "text": "Next page"}, PermissionLevel.LOW_RISK),
        ({"kind": "submit", "text": "Search", "form": "/search"}, PermissionLevel.LOW_RISK),
        ({"kind": "submit", "text": "Go", "form": "/contact"}, PermissionLevel.CONFIRM),
        ({"kind": "button", "text": "Buy now"}, PermissionLevel.CONFIRM),
        ({"kind": "button", "text": "Add to cart"}, PermissionLevel.CONFIRM),
        ({"kind": "button", "text": "Sign in"}, PermissionLevel.CONFIRM),
        ({"kind": "button", "text": "Delete account"}, PermissionLevel.CONFIRM),
        ({"kind": "button", "text": "Pay now"}, PermissionLevel.SENSITIVE),
        ({"kind": "link", "text": "Transfer money"}, PermissionLevel.SENSITIVE),
        ({"kind": "button", "text": "", "label": "Pay"}, PermissionLevel.SENSITIVE),
    ],
)
def test_click_levels(element, level):
    assert click_level(element) == level


@pytest.mark.parametrize(
    ("field", "level"),
    [
        ({"type": "search", "name": "q"}, PermissionLevel.LOW_RISK),
        ({"type": "text", "label": "Search the docs"}, PermissionLevel.LOW_RISK),
        ({"type": "email", "label": "Your email"}, PermissionLevel.CONFIRM),
        ({"type": "text", "name": "full_name"}, PermissionLevel.CONFIRM),
        ({"type": "password", "name": "pw"}, PermissionLevel.SENSITIVE),
        ({"type": "text", "label": "Card number"}, PermissionLevel.SENSITIVE),
        (
            {"type": "text", "autocomplete": "one-time-code", "label": "OTP"},
            PermissionLevel.SENSITIVE,
        ),
        ({"type": "text", "label": "CVV"}, PermissionLevel.SENSITIVE),
    ],
)
def test_type_levels(field, level):
    assert type_level(field, submit=False) == level


@pytest.mark.parametrize(
    ("text", "level"),
    [
        ("python release notes", PermissionLevel.LOW_RISK),
        ("fixtures", PermissionLevel.LOW_RISK),
        ("Hunter2pass", PermissionLevel.CONFIRM),  # a password typed into the search box
        ("4111 1111 1111 1111", PermissionLevel.SENSITIVE),  # a card number: never
    ],
)
def test_typed_text_can_raise_the_level(text, level):
    search_box = {"type": "search", "name": "q"}
    assert type_level(search_box, submit=True, text=text) == level


# ---------- real browser ----------

PAGES = {
    "/": """<html><head><title>Fixture Shop</title></head><body>
        <h1>Welcome to the fixture shop</h1>
        <p>Python 3.14.7 is the latest release.</p>
        <a href="/about">About us</a>
        <form action="/search"><input type="search" name="q" aria-label="Search"></form>
        <button id="buy">Buy now</button>
        <form action="/login"><input type="password" name="pw" aria-label="Password">
          <input type="submit" value="Sign in"></form>
        <img src="http://10.0.0.1/tracker.png">
        </body></html>""",
    "/about": "<title>About</title><body>We sell fixtures since 1999.</body>",
    "/search": "<title>Results</title><body>Results for your query</body>",
    "/evil": """<html><head><title>Evil</title></head><body>
        <a href="http://192.168.1.1/admin">Router admin</a></body></html>""",
}


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 (http.server's naming)
        body = PAGES.get(self.path.split("?")[0])
        self.send_response(200 if body else 404)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write((body or "not found").encode())

    def log_message(self, *args):
        pass


def _chromium_installed() -> bool:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            return Path(p.chromium.executable_path).exists()
    except Exception:
        return False


needs_chromium = pytest.mark.skipif(not _chromium_installed(), reason="Chromium not installed")


@pytest.fixture(scope="module")
def site():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


@pytest.fixture
async def browser(site):
    async def only_the_fixture_site(url: str) -> None:
        if not url.startswith(site):
            raise UnsafeUrlError("private address")

    agent = BrowserAgent(url_checker=only_the_fixture_site)
    yield agent
    await agent.close()


def registry_for(agent: BrowserAgent) -> ToolRegistry:
    registry = ToolRegistry(PermissionPolicy(), None)
    for tool in (BrowserOpenTool, BrowserFindTextTool, BrowserClickTool, BrowserTypeTool):
        registry.register(tool(agent))
    return registry


def element_id(output: dict, words: str) -> int:
    line = next(e for e in output["elements"] if words in e)
    return int(line[1 : line.index("]")])


@needs_chromium
async def test_open_shows_text_and_numbered_elements(browser, site):
    registry = registry_for(browser)
    result = await registry.execute("browser_open", {"url": site + "/"})
    assert result.status == "ok", result.error
    page = result.output
    assert page["title"] == "Fixture Shop"
    assert "3.14.7" in page["text"]
    assert any("link: About us" in e for e in page["elements"])
    assert any("Buy now" in e for e in page["elements"])
    assert "DATA" in page["note"]


@needs_chromium
async def test_safe_click_runs_and_risky_click_asks(browser, site):
    registry = registry_for(browser)
    page = (await registry.execute("browser_open", {"url": site + "/"})).output

    buy = await registry.execute("browser_click", {"element": element_id(page, "Buy now")})
    assert buy.status == "needs_confirmation"
    assert "Buy now" in buy.preview

    about = await registry.execute("browser_click", {"element": element_id(page, "About us")})
    assert about.status == "ok", about.error
    assert about.output["title"] == "About"


@needs_chromium
async def test_search_box_types_freely_but_password_is_refused(browser, site):
    registry = registry_for(browser)
    page = (await registry.execute("browser_open", {"url": site + "/"})).output

    password = await registry.execute(
        "browser_type",
        {"element": element_id(page, "Password"), "text": "hunter2"},
        ToolContext(confirmed=True),  # even the user's "yes" can't unlock level 3
    )
    assert password.status == "denied"

    search = await registry.execute(
        "browser_type", {"element": element_id(page, "Search"), "text": "fixtures", "submit": True}
    )
    assert search.status == "ok", search.error
    assert search.output["title"] == "Results"


@needs_chromium
async def test_private_addresses_are_blocked(browser, site):
    with pytest.raises(BrowserError, match="Can't open"):
        await browser.open("http://192.168.1.1/")

    await browser.open(site + "/")  # the page's <img> points at 10.0.0.1
    assert any("10.0.0.1" in url for url in browser.blocked_requests)

    page = await browser.open(site + "/evil")
    router = next(e["id"] for e in page.elements if "Router" in e["text"])
    with pytest.raises(BrowserError, match="Blocked for safety"):
        await browser.click(router)


@needs_chromium
async def test_find_text_and_unknown_element(browser, site):
    registry = registry_for(browser)
    await registry.execute("browser_open", {"url": site + "/"})
    found = await registry.execute("browser_find_text", {"query": "latest release"})
    assert "3.14.7" in found.output["matches"][0]

    missing = await registry.execute("browser_click", {"element": 99})
    assert missing.status == "error"


@needs_chromium
async def test_typing_into_a_link_is_refused_with_a_hint(browser, site):
    registry = registry_for(browser)
    page = (await registry.execute("browser_open", {"url": site + "/"})).output
    wrong = await registry.execute(
        "browser_type", {"element": element_id(page, "About us"), "text": "demo"}
    )
    assert wrong.status == "error"  # refused before any confirmation question
    assert "not a text field" in wrong.error
    assert "Search" in wrong.error


async def test_real_checker_refuses_localhost():
    """Default checker (no browser started): loopback is refused before Chromium even runs."""
    agent = BrowserAgent()
    with pytest.raises(BrowserError):
        await agent.open("http://127.0.0.1:8000/")
    with pytest.raises(BrowserError):
        await agent.open("file:///C:/Windows/win.ini")
    await agent.close()


def test_elements_survive_the_result_size_limit():
    """The agent loop cuts tool results at 4000 chars - the numbered elements must fit."""
    import json

    from app.browser.agent import PageSnapshot
    from app.tools.browser_tools import describe

    elements = [{"id": i, "kind": "link", "text": f"Link number {i}"} for i in range(1, 61)]
    snapshot = PageSnapshot(
        url="https://x.test/", title="Long", text="word " * 5000, elements=elements
    )
    result = describe(snapshot)
    assert len(json.dumps(result, ensure_ascii=False)) <= 4000
    assert result["elements"][-1] == "[60] link: Link number 60"
    assert result["text_truncated"]


@needs_chromium
async def test_browser_restarts_after_it_was_closed(browser, site):
    await browser.open(site + "/")
    await browser._call(browser._browser.close)  # simulate a crash
    page = await browser.open(site + "/about")
    assert page.title == "About"
