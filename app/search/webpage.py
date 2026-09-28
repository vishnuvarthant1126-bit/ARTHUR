"""Read a public web page as plain text - safely.

SSRF (Server-Side Request Forgery) protection: if ARTHUR fetched *any* URL, a
malicious page or prompt could make it open http://192.168.1.1/admin (your
router), http://localhost:11434 (your own Ollama) or a cloud metadata address.
So before connecting - and again for every redirect - we resolve the host name
and refuse anything that isn't a public internet address.

Other limits: only http/https, at most 3 redirects, at most `max_bytes`
downloaded, only HTML or plain text, 10 s timeout. The page's text is returned
as untrusted data.
"""

import asyncio
import ipaddress
import re
import socket
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import httpx
from pydantic import BaseModel

MAX_REDIRECTS = 3
ALLOWED_TYPES = ("text/html", "text/plain", "application/xhtml+xml")


class UnsafeUrlError(Exception):
    """The URL points somewhere ARTHUR must not go."""


class FetchError(Exception):
    """The page couldn't be fetched or read (safe to show the user)."""


class PageText(BaseModel):
    url: str
    title: str
    text: str
    truncated: bool


async def check_public_url(url: str) -> None:
    """Raise UnsafeUrlError unless the URL is http(s) and resolves only to public IPs."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise UnsafeUrlError(f"Only http and https links are allowed (got '{parts.scheme}:').")
    host = parts.hostname
    if not host:
        raise UnsafeUrlError("The link has no host name.")
    if parts.username or parts.password:
        raise UnsafeUrlError("Links with embedded usernames or passwords are not allowed.")
    if host.lower() == "localhost" or host.lower().endswith((".localhost", ".local", ".internal")):
        raise UnsafeUrlError("Local addresses are not allowed.")

    try:
        infos = await asyncio.get_running_loop().getaddrinfo(
            host, parts.port or (443 if parts.scheme == "https" else 80), type=socket.SOCK_STREAM
        )
    except socket.gaierror as exc:
        raise FetchError(f"Couldn't find the website '{host}'.") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global or ip.is_multicast:
            raise UnsafeUrlError(
                f"'{host}' points to a private or local network address - blocked for safety."
            )


async def fetch_page(
    url: str, client: httpx.AsyncClient, *, max_bytes: int = 2 * 1024 * 1024, max_chars: int = 6000
) -> PageText:
    for _ in range(MAX_REDIRECTS + 1):
        await check_public_url(url)
        try:
            async with client.stream("GET", url, follow_redirects=False, timeout=10.0) as response:
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
