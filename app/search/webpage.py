"""Read a public web page as plain text - safely.

SSRF (Server-Side Request Forgery) protection: if ARTHUR fetched *any* URL, a
malicious page or prompt could make it open http://192.168.1.1/admin (your
router), http://localhost:11434 (your own Ollama) or a cloud metadata address.
So before connecting - and again for every redirect - we resolve the host name,
refuse anything that isn't a public internet address, and then connect to exactly
the address we checked (see `pinned_request`; the rules live in app/security/network.py).

Other limits: only http/https, at most 3 redirects, at most `max_bytes`
downloaded, only HTML or plain text, 10 s timeout. The page's text is returned
as untrusted data.
"""

import re
from collections.abc import Awaitable, Callable
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
from pydantic import BaseModel

from app.security.network import LookupFailed, Target, UnsafeUrlError, resolve_public

__all__ = [
    "FetchError",
    "PageText",
    "UnsafeUrlError",
    "check_public_url",
    "fetch_page",
    "new_page_client",
]

MAX_REDIRECTS = 3
ALLOWED_TYPES = ("text/html", "text/plain", "application/xhtml+xml")


class FetchError(Exception):
    """The page couldn't be fetched or read (safe to show the user)."""


class PageText(BaseModel):
    url: str
    title: str
    text: str
    truncated: bool


Resolver = Callable[[str], Awaitable[Target]]


def new_page_client(user_agent: str = "ARTHUR/0.1 (personal assistant)") -> httpx.AsyncClient:
    """The HTTP client for reading web pages: it never keeps connections open.

    With pinned requests the connection pool only sees IP addresses. A kept-alive
    connection that was TLS-verified for site A could otherwise be reused for site B on
    the same address, skipping B's certificate check (found in a live test).
    """
    return httpx.AsyncClient(
        headers={"User-Agent": user_agent}, limits=httpx.Limits(max_keepalive_connections=0)
    )


async def check_public_url(url: str) -> None:
    """Raise UnsafeUrlError unless the URL is http(s) and resolves only to public IPs."""
    try:
        await resolve_public(url)
    except LookupFailed as exc:
        raise FetchError(str(exc)) from exc


def pinned_request(url: str, target: Target) -> tuple[str, dict[str, str], dict[str, str]]:
    """Build a request that connects to the CHECKED address, not to whatever the name
    resolves to a moment later (DNS rebinding).

    The URL carries the IP address; the Host header and the TLS name (SNI, also used to
    verify the certificate) still carry the real host name, so the website works normally.
    """
    parts = urlsplit(url)
    address = f"[{target.ip}]" if ":" in target.ip else target.ip
    default_port = 443 if target.scheme == "https" else 80
    port = "" if target.port == default_port else f":{target.port}"
    pinned = urlunsplit((target.scheme, address + port, parts.path or "/", parts.query, ""))
    extensions = {"sni_hostname": target.host} if target.scheme == "https" else {}
    return pinned, {"Host": target.host + port}, extensions


async def fetch_page(
    url: str,
    client: httpx.AsyncClient,
    *,
    max_bytes: int = 2 * 1024 * 1024,
    max_chars: int = 6000,
    resolver: Resolver = resolve_public,
) -> PageText:
    for _ in range(MAX_REDIRECTS + 1):
        try:
            target = await resolver(url)  # looked up once, checked...
        except LookupFailed as exc:
            raise FetchError(str(exc)) from exc
        pinned, headers, extensions = pinned_request(url, target)  # ...and used as is
        try:
            async with client.stream(
                "GET",
                pinned,
                headers=headers,
                extensions=extensions,
                follow_redirects=False,
                timeout=10.0,
            ) as response:
                if response.is_redirect:
                    location = response.headers.get("location", "")
                    url = urljoin(url, location)  # check the new target on the next loop
                    continue
                if response.status_code != 200:
                    raise FetchError(f"The page returned HTTP {response.status_code}.")
                content_type = response.headers.get("content-type", "").split(";")[0].strip()
                if content_type and content_type not in ALLOWED_TYPES:
                    raise FetchError(f"Not a web page (content type '{content_type}').")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > max_bytes:
                        break
                encoding = response.encoding or "utf-8"
        except httpx.HTTPError as exc:
            raise FetchError(f"Couldn't load the page ({type(exc).__name__}).") from exc

        raw = bytes(body[:max_bytes]).decode(encoding, errors="replace")
        if content_type == "text/plain":
            title, text = "", _tidy(raw)
        else:
            title, text = html_to_text(raw)
        return PageText(
            url=url, title=title, text=text[:max_chars], truncated=len(text) > max_chars
        )
    raise FetchError("Too many redirects.")


class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "template", "iframe", "head", "nav", "footer"}
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title = ""
        self._skip_depth = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self._in_title = True
        elif tag in self.SKIP:
            self._skip_depth += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag in self.SKIP and self._skip_depth:
            self._skip_depth -= 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self._skip_depth:
            self.parts.append(data)


def html_to_text(html: str) -> tuple[str, str]:
    """(title, readable text) - scripts, styles, navigation and footers removed."""
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    return " ".join(parser.title.split()), _tidy("".join(parser.parts))


def _tidy(text: str) -> str:
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r" *\n[ \n]*", "\n", text)
    return text.strip()
