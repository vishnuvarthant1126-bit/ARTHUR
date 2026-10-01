"""Where ARTHUR may connect: public addresses only, and exactly the address it checked.

The attack is "DNS rebinding": a name gives a public address when checked and a private
one (your router, localhost) when really used. Pinning the checked address stops it.
"""

import asyncio

import httpx
import pytest

from app.search.webpage import fetch_page, pinned_request
from app.security.egress_proxy import EgressProxy, port_allowed
from app.security.network import (
    Target,
    UnsafeUrlError,
    is_public,
    resolve_public,
)

# ---------- which addresses are public ----------


@pytest.mark.parametrize(
    ("ip", "public"),
    [
        ("93.184.215.14", True),
        ("2606:2800:21f:cb07:6820:80da:af6b:8b2c", True),
        ("127.0.0.1", False),
        ("10.0.0.5", False),
        ("172.16.0.1", False),
        ("192.168.1.1", False),  # your router
        ("169.254.169.254", False),  # cloud metadata service
        ("0.0.0.0", False),
        ("::1", False),
        ("fe80::1", False),
        ("::ffff:192.168.1.1", False),  # a private IPv4 address dressed up as IPv6
        ("224.0.0.1", False),
        ("100.64.0.1", False),  # carrier-grade NAT
    ],
)
def test_is_public(ip, public):
    assert is_public(ip) is public


@pytest.mark.parametrize(
    "url",
    [
        "file:///C:/Windows/win.ini",
        "ftp://example.com/x",
        "http://localhost:11434/api/tags",  # your own Ollama
        "http://printer.local/",
        "http://user:pass@93.184.215.14/",
        "http://127.0.0.1:8000/",
        "http://[::1]/",
        "http://192.168.1.1/admin",
        "http://169.254.169.254/latest/meta-data/",
    ],
)
async def test_unsafe_urls_are_refused(url):
    with pytest.raises(UnsafeUrlError):
        await resolve_public(url)


@pytest.mark.parametrize(
    "url", ["http://2130706433/", "http://0x7f.0.0.1/", "http://017700000001/"]
)
async def test_disguised_loopback_addresses_are_refused(url):
    """127.0.0.1 written as one number, in hexadecimal or octal. Linux reads these as
    127.0.0.1, Windows tries a slow DNS lookup. ARTHUR refuses them itself, without any
    lookup - so the answer is the same (and instant) on every system."""
    with pytest.raises(UnsafeUrlError, match="Unusual numeric"):
        await resolve_public(url)


@pytest.mark.parametrize("host", ["bbc.de", "fb.cc", "cafe.de", "example.com", "1e100.net"])
async def test_real_names_are_not_mistaken_for_numbers(monkeypatch, host):
    """Names made only of the letters a-f (bbc.de, fb.cc) look "hexadecimal" but are real
    websites: they must reach the normal lookup, not be refused as numeric addresses."""
    import asyncio

    looked_up = []

    async def fake_getaddrinfo(name, port, **kwargs):
        looked_up.append(name)
        return [(2, 1, 6, "", ("93.184.215.14", port))]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", fake_getaddrinfo)
    target = await resolve_public(f"https://{host}/")
    assert looked_up == [host] and target.ip == "93.184.215.14"


# ---------- read_webpage: the checked address is the one that gets used ----------


def test_pinned_request_keeps_the_name_but_connects_to_the_checked_address():
    target = Target("https", "example.com", "93.184.215.14", 443)
    url, headers, extensions = pinned_request("https://example.com/a/b?q=1", target)
    assert url == "https://93.184.215.14/a/b?q=1"
    assert headers == {"Host": "example.com"}
    assert extensions == {"sni_hostname": "example.com"}  # the certificate must match the NAME

    odd_port = Target("http", "example.com", "2606:2800::1", 8080)
    assert pinned_request("http://example.com:8080/", odd_port)[:2] == (
        "http://[2606:2800::1]:8080/",
        {"Host": "example.com:8080"},
    )


async def test_rebinding_cannot_redirect_a_page_fetch():
    """The name is looked up exactly once; the request goes to that address."""
    lookups = []
    seen = []

    async def rebinding_dns(url: str) -> Target:
        lookups.append(url)
        if len(lookups) > 1:  # a second lookup would get the private address
            raise UnsafeUrlError("points to a private address")
        return Target("http", "evil.example", "93.184.215.14", 80)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.host, request.headers["host"]))
        return httpx.Response(200, html="<title>ok</title><p>hello</p>")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    page = await fetch_page("http://evil.example/", client, resolver=rebinding_dns)

    assert page.title == "ok"
    assert lookups == ["http://evil.example/"]
    assert seen == [("93.184.215.14", "evil.example")]


async def test_every_redirect_hop_is_checked():
    async def dns(url: str) -> Target:
        if "192.168" in url:
            raise UnsafeUrlError("points to a private address")
        return Target("http", "site.example", "93.184.215.14", 80)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "http://192.168.1.1/admin"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(UnsafeUrlError):
        await fetch_page("http://site.example/", client, resolver=dns)


# ---------- the browser's egress proxy ----------


@pytest.fixture
async def upstream():
    """A tiny "website" that answers every request and records what it received."""
    received = []

    async def handle(reader, writer):
        data = await reader.readuntil(b"\r\n\r\n")
        received.append(data.decode())
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\nConnection: close\r\n\r\nhello")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    yield server.sockets[0].getsockname()[1], received
    server.close()
    await server.wait_closed()


@pytest.fixture
async def proxy(upstream):
    port, _ = upstream

    async def test_internet(scheme: str, host: str, port_wanted: int) -> Target:
        if host == "shop.test":
            return Target(scheme, host, "127.0.0.1", port)  # the "public" test site
        raise UnsafeUrlError(f"'{host}' points to a private address")

    proxy = EgressProxy(test_internet)
    await proxy.start()
    yield proxy
    await proxy.stop()


async def ask(proxy: EgressProxy, request: str) -> str:
    reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
    writer.write(request.encode())
    await writer.drain()
    response = await asyncio.wait_for(reader.read(4096), 5)
    writer.close()
    return response.decode()


async def test_proxy_forwards_plain_http_to_the_checked_address(proxy, upstream):
    _, received = upstream
    response = await ask(
        proxy,
        "GET http://shop.test/about?x=1 HTTP/1.1\r\nHost: shop.test\r\n"
        "Proxy-Connection: keep-alive\r\n\r\n",
    )
    assert response.startswith("HTTP/1.1 200 OK")
    assert response.endswith("hello")
    assert received[0].startswith("GET /about?x=1 HTTP/1.1\r\n")  # rewritten for the website
    assert "Connection: close" in received[0]  # so every request is checked on its own
    assert "Proxy-Connection" not in received[0]
    assert proxy.connections == 1


async def test_proxy_tunnels_https_connections(proxy, upstream):
    reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
    writer.write(b"CONNECT shop.test:443 HTTP/1.1\r\nHost: shop.test:443\r\n\r\n")
    assert (await reader.readuntil(b"\r\n\r\n")).startswith(b"HTTP/1.1 200")
    # From here the proxy only copies bytes (a real browser would now start TLS).
    writer.write(b"GET / HTTP/1.1\r\nHost: shop.test\r\n\r\n")
    assert (await asyncio.wait_for(reader.read(4096), 5)).endswith(b"hello")
    writer.close()


@pytest.mark.parametrize(
    "request_text",
    [
        "CONNECT router.home:443 HTTP/1.1\r\n\r\n",  # resolves to a private address
        "CONNECT 192.168.1.1:443 HTTP/1.1\r\n\r\n",
        "GET http://127.0.0.1:11434/api/tags HTTP/1.1\r\n\r\n",  # your own Ollama
        "CONNECT shop.test:25 HTTP/1.1\r\n\r\n",  # mail port
        "CONNECT shop.test:22 HTTP/1.1\r\n\r\n",  # SSH
        "GET ftp://shop.test/file HTTP/1.1\r\n\r\n",
        "GET /etc/passwd HTTP/1.1\r\n\r\n",  # not a proxy request at all
    ],
)
async def test_proxy_refuses(proxy, request_text):
    response = await ask(proxy, request_text)
    assert response.startswith("HTTP/1.1 403")
    assert proxy.connections == 0
    assert len(proxy.blocked) == 1


async def test_proxy_survives_garbage(proxy):
    assert (await ask(proxy, "\x00\x01garbage\r\n\r\n")).startswith("HTTP/1.1 400")
    assert (await ask(proxy, "GET http://shop.test/ HTTP/1.1\r\n\r\n")).startswith("HTTP/1.1 200")


def test_proxy_ports():
    assert port_allowed(80) and port_allowed(443) and port_allowed(8080)
    assert not port_allowed(22) and not port_allowed(25) and not port_allowed(445)
