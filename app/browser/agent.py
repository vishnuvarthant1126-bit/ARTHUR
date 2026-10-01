"""ARTHUR's own browser (Playwright + Chromium), isolated and guarded.

- Its own private profile: no cookies, logins or history, no access to your Chrome.
- Downloads disabled; service workers blocked.
- EVERY request the page makes (page, images, scripts, redirects) goes through
  `_guard`, which refuses private/local addresses (SSRF protection, as in Phase 10).
- EVERY connection Chromium opens goes through ARTHUR's own egress proxy
  (app/security/egress_proxy.py), which looks the name up itself and connects only to
  the checked address - so a name can't change its answer between check and use.
- After each action ARTHUR takes a *snapshot*: page text plus numbered links,
  buttons and fields, so the model can say "click [3]" instead of guessing.

Windows detail: Playwright starts the browser as a subprocess, which needs a
"Proactor" event loop - but `uvicorn --reload` uses a different loop on Windows.
So the browser lives in its own thread with its own loop, and the rest of ARTHUR
sends it work with `_call()`.
"""

import asyncio
import contextlib
import sys
import threading
from collections.abc import Callable
from urllib.parse import urlsplit

from pydantic import BaseModel

from app.observability.logging import get_logger
from app.security.egress_proxy import EgressProxy, HostResolver
from app.security.network import (
    LookupFailed,
    UnsafeUrlError,
    resolve_public,
    resolve_public_host,
)

log = get_logger(__name__)

MAX_ELEMENTS = 60
MAX_PAGE_CHARS = 20_000

# Runs inside the page: lists visible links/buttons/fields and tags them with numbers.
SNAPSHOT_JS = r"""
() => {
  const clean = (t) => (t || "").replace(/\s+/g, " ").trim().slice(0, 100);
  const visible = (el) => {
    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    if (r.right <= 0 || r.bottom <= 0) return false;  // parked off-screen ("skip" links)
    return r.width > 1 && r.height > 1 && s.visibility !== "hidden" && s.display !== "none";
  };
  document.querySelectorAll("[data-arthur-id]").forEach((e) => e.removeAttribute("data-arthur-id"));
  const elements = [];
  const candidates = document.querySelectorAll(
    "a[href], button, input, textarea, select, [role=button], [role=link]");
  for (const el of candidates) {
    if (elements.length >= %MAX% || !visible(el)) continue;
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute("type") || "").toLowerCase();
    const role = el.getAttribute("role");
    if (type === "hidden") continue;
    let kind;
    if (tag === "a" || role === "link") kind = "link";
    else if (tag === "button" || role === "button" ||
             ["submit", "button", "reset", "image"].includes(type)) {
      const submits = type === "submit" || (tag === "button" && type !== "button" && el.form);
      kind = submits ? "submit" : "button";
    } else kind = "field";
    const id = elements.length + 1;
    el.setAttribute("data-arthur-id", String(id));
    const label = kind === "field"
      ? clean((el.labels && el.labels[0] && el.labels[0].innerText) ||
              el.getAttribute("aria-label") || el.placeholder)
      : clean(el.getAttribute("aria-label") || el.title);
    const text = kind === "field" ? "" : clean(el.innerText || el.value);
    const f = el.form;
    const form = f ? clean([f.getAttribute("action"), f.id, f.getAttribute("name")].join(" "))
                   : null;
    elements.push({ id, kind, text, label, href: tag === "a" ? el.href : null, type: type || tag,
      name: el.getAttribute("name"), id_attr: el.id || null, placeholder: el.placeholder || null,
      autocomplete: el.getAttribute("autocomplete"), form });
  }
  const text = document.body ? document.body.innerText.replace(/\n{3,}/g, "\n\n") : "";
  return { title: document.title, text: text.slice(0, %CHARS%), elements };
}
""".replace("%MAX%", str(MAX_ELEMENTS)).replace("%CHARS%", str(MAX_PAGE_CHARS))


class BrowserError(Exception):
    """A browser problem that is safe to show the user."""


class PageSnapshot(BaseModel):
    url: str
    title: str
    text: str
    elements: list[dict]


class BrowserAgent:
    def __init__(
        self,
        *,
        headless: bool = True,
        resolver: HostResolver | None = None,
        timeout_ms: int = 15_000,
        on_block: Callable[[], None] = lambda: None,
    ) -> None:
        self.on_block = on_block  # a request was refused (counted in the metrics)
        self.headless = headless
        # None = real DNS, public addresses only. Tests pass a fake (scheme, host, port) rule.
        self.resolver = resolver
        # ALL of Chromium's traffic goes through this proxy, which looks names up itself
        # and connects only to the checked address (closes the DNS-rebinding gap).
        self.proxy = EgressProxy(resolver or resolve_public_host, on_block)
        self.timeout_ms = timeout_ms
        self.blocked_requests: list[str] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._pw = self._browser = self._context = self._page = None
        self._host_checks: dict[str, str | None] = {}  # host -> error message (None = ok)
        self._elements: dict[int, dict] = {}
        self._last_text = ""

    # ---------- public API (callable from ARTHUR's normal event loop) ----------

    async def open(self, url: str) -> PageSnapshot:
        return await self._call(self._open, url)

    async def click(self, element_id: int) -> PageSnapshot:
        return await self._call(self._click, element_id)

    async def type_text(self, element_id: int, text: str, submit: bool) -> PageSnapshot:
        return await self._call(self._type, element_id, text, submit)

    async def find_text(self, query: str, limit: int = 8) -> list[str]:
        words = [w.lower() for w in query.split() if len(w) > 1]
        lines = [line.strip() for line in self._last_text.splitlines() if line.strip()]
        scored = [(sum(w in line.lower() for w in words), line) for line in lines]
        return [line[:300] for score, line in sorted(scored, key=lambda s: -s[0]) if score][:limit]

    def element(self, element_id: int) -> dict | None:
        """What the last snapshot said about element [id] (used to judge risk before acting)."""
        return self._elements.get(element_id)

    def elements(self) -> list[dict]:
        return list(self._elements.values())

    @property
    def current_url(self) -> str | None:
        return self._page.url if self._page else None

    async def close(self) -> None:
        if self._loop is None:
            return
        try:
            await self._call(self._shutdown)
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=5)
            self._loop = None

    # ---------- the browser's own thread and loop ----------

    async def _call(self, fn, *args):
        if self._loop is None:
            self._start_thread()
        future = asyncio.run_coroutine_threadsafe(fn(*args), self._loop)
        return await asyncio.wrap_future(future)

    def _start_thread(self) -> None:
        loop = asyncio.ProactorEventLoop() if sys.platform == "win32" else asyncio.new_event_loop()
        thread = threading.Thread(target=loop.run_forever, name="arthur-browser", daemon=True)
        thread.start()
        self._loop, self._thread = loop, thread

    # ---------- everything below runs in the browser thread ----------

    async def _ensure_page(self) -> None:
        if (
            self._page is not None
            and not self._page.is_closed()
            and self._browser is not None
            and self._browser.is_connected()
        ):
            return
        await self._shutdown()  # clean up a crashed/closed browser before starting fresh
        from playwright.async_api import async_playwright

        self._pw = await async_playwright().start()
        try:
            if self.proxy.port is None:
                await self.proxy.start()
            self._browser = await self._pw.chromium.launch(
                headless=self.headless,
                # "<-loopback>": even localhost addresses go through the proxy (and are refused)
                proxy={"server": f"http://127.0.0.1:{self.proxy.port}", "bypass": "<-loopback>"},
                args=[
                    "--disable-quic",  # QUIC is UDP and would not use the proxy
                    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
                ],
            )
        except Exception as exc:
            raise BrowserError(
                "ARTHUR's browser isn't installed. Run: python -m playwright install chromium"
            ) from exc
        self._context = await self._browser.new_context(
            accept_downloads=False, service_workers="block", locale="en-US"
        )
        self._context.set_default_timeout(self.timeout_ms)
        await self._context.route("**/*", self._guard)
        self._page = await self._context.new_page()
        log.info("browser_started", headless=self.headless)

    async def _guard(self, route) -> None:
        """Every request the page makes passes here. Private/local targets are refused."""
        url = route.request.url
        if url.startswith(("data:", "blob:", "about:")):
            await route.continue_()
            return
        problem = await self._check_host(url)
        if problem:
            self.blocked_requests.append(url)
            self.on_block()
            log.warning("browser_request_blocked", url=url[:200], reason=problem)
            await route.abort("blockedbyclient")
        else:
            await route.continue_()

    async def _check_host(self, url: str) -> str | None:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https"):
            return f"scheme '{parts.scheme}' not allowed"
        key = f"{parts.scheme}://{parts.netloc}".lower()
        if key not in self._host_checks:  # one DNS check per host, not per image
            try:
                if self.resolver is None:
                    await resolve_public(url)
                else:
                    default_port = 443 if parts.scheme == "https" else 80
                    await self.resolver(
                        parts.scheme, parts.hostname or "", parts.port or default_port
                    )
                self._host_checks[key] = None
            except (UnsafeUrlError, LookupFailed, ValueError) as exc:
                self._host_checks[key] = str(exc)
        return self._host_checks[key]

    async def _open(self, url: str) -> PageSnapshot:
        if "://" not in url and not url.startswith(("file:", "javascript:", "data:")):
            url = "https://" + url  # "python.org" -> "https://python.org"
        problem = await self._check_host(url)
        if problem:
            raise BrowserError(f"Can't open that address: {problem}")
        await self._ensure_page()
        await self._navigate(lambda: self._page.goto(url, wait_until="domcontentloaded"))
        return await self._snapshot()

    async def _click(self, element_id: int) -> PageSnapshot:
        await self._ensure_page()
        target = self._page.locator(f'[data-arthur-id="{element_id}"]')
        if await target.count() == 0:
            raise BrowserError(f"There is no element [{element_id}] on the current page.")
        await self._navigate(lambda: target.first.click(timeout=5000))
        return await self._snapshot()

    async def _type(self, element_id: int, text: str, submit: bool) -> PageSnapshot:
        await self._ensure_page()
        field = self._page.locator(f'[data-arthur-id="{element_id}"]')
        if await field.count() == 0:
            raise BrowserError(f"There is no field [{element_id}] on the current page.")
        await field.first.fill(text, timeout=5000)
        if submit:
            await self._navigate(lambda: field.first.press("Enter"))
        return await self._snapshot()

    async def _navigate(self, action) -> None:
        from playwright.async_api import Error as PlaywrightError

        proxy_blocked_before = len(self.proxy.blocked)
        blocked_before = len(self.blocked_requests) + proxy_blocked_before
        try:
            await action()
            # Give JavaScript-heavy pages a moment to settle, but don't wait forever.
            with contextlib.suppress(PlaywrightError):
                await self._page.wait_for_load_state("networkidle", timeout=3000)
            # A click that navigates somewhere blocked doesn't raise - it lands on an error page.
            if self._page.url.startswith("chrome-error://"):
                blocked = len(self.blocked_requests) + len(self.proxy.blocked) > blocked_before
                await self._page.go_back(wait_until="domcontentloaded")
                if blocked:
                    raise BrowserError(
                        "Blocked for safety: that leads to a private or local address."
                    )
                raise BrowserError("That page could not be loaded.")
            # A plain-http page refused by the proxy arrives as an ordinary "403" page.
            if self._page.url in self.proxy.blocked[proxy_blocked_before:]:
                with contextlib.suppress(PlaywrightError):
                    await self._page.go_back(wait_until="domcontentloaded")
                raise BrowserError("Blocked for safety: that leads to a private or local address.")
        except PlaywrightError as exc:
            message = str(exc).splitlines()[0]
            if "ERR_BLOCKED_BY_CLIENT" in message or "ERR_TUNNEL_CONNECTION_FAILED" in message:
                raise BrowserError(
                    "Blocked for safety: that leads to a private or local address."
                ) from exc
            if "Timeout" in message:
                raise BrowserError("The page took too long to respond.") from exc
            raise BrowserError(f"Browser error: {message[:200]}") from exc

    async def _snapshot(self) -> PageSnapshot:
        data = await self._page.evaluate(SNAPSHOT_JS)
        self._elements = {e["id"]: e for e in data["elements"]}
        self._last_text = data["text"]
        snapshot = PageSnapshot(
            url=self._page.url, title=data["title"], text=data["text"], elements=data["elements"]
        )
        log.info(
            "browser_snapshot",
            url=snapshot.url[:200],
            elements=len(snapshot.elements),
            chars=len(snapshot.text),
        )
        return snapshot

    async def _shutdown(self) -> None:
        for closer in (self._context, self._browser):
            if closer is not None:
                with contextlib.suppress(Exception):  # may already be gone
                    await closer.close()
        if self._pw is not None:
            with contextlib.suppress(Exception):
                await self._pw.stop()
        self._pw = self._browser = self._context = self._page = None
        await self.proxy.stop()
        self.proxy.port = None
