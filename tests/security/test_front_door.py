"""Who may talk to ARTHUR: Host allow-list, Origin check, rate limits, security headers.

Each test is an attack that must fail. See docs/SECURITY.md for the threat list.
"""

import httpx
import pytest
from starlette.websockets import WebSocketDisconnect

from app.security.network import host_allowed, host_name
from app.security.rate_limit import RateLimiter, group_for

# ---------- Host allow-list (DNS rebinding against ARTHUR itself) ----------


@pytest.mark.parametrize(
    ("header", "allowed"),
    [
        ("localhost:8000", True),
        ("127.0.0.1:8000", True),
        ("LOCALHOST", True),
        ("[::1]:8000", True),
        ("evil.example:8000", False),  # a foreign name pointed at 127.0.0.1
        ("127.0.0.1.evil.example", False),
        ("localhost.evil.example:8000", False),
        ("192.168.1.20:8000", False),  # the LAN address: not served unless configured
        ("", False),
        (None, False),
    ],
)
def test_host_allow_list(header, allowed):
    assert host_allowed(header) is allowed


def test_extra_hosts_must_be_configured_explicitly():
    assert not host_allowed("arthur.home:8000")
    assert host_allowed("arthur.home:8000", frozenset({"arthur.home"}))
    assert host_name("[::1]:8000") == "::1"


async def test_rebound_website_cannot_use_the_api(client):
    """evil.example resolves to 127.0.0.1, so the browser sends its requests to ARTHUR.
    Origin and Host both say evil.example - only the Host allow-list notices."""
    rebound = {"Host": "evil.example:8000", "Origin": "http://evil.example:8000"}

    read = await client.get("/memories", headers=rebound)
    write = await client.post("/chat", json={"message": "delete my files"}, headers=rebound)

    assert read.status_code == write.status_code == 421
    assert read.json()["error"]["type"] == "wrong_host"


def test_rebound_website_cannot_open_the_websocket(ws_client):
    with (
        pytest.raises(WebSocketDisconnect),
        ws_client.websocket_connect("/ws", headers={"Host": "evil.example:8000"}),
    ):
        pass


# ---------- Origin check (CSRF) ----------


async def test_other_websites_cannot_change_anything(client):
    forged = {"Origin": "https://evil.example"}
    response = await client.post("/reminders", json={"text": "x", "when": "5pm"}, headers=forged)
    assert response.status_code == 403
    assert (await client.get("/reminders")).json()["upcoming"] == []


# ---------- rate limits ----------


def test_rate_limiter_sliding_window():
    now = [0.0]
    limiter = RateLimiter({"chat": 3}, clock=lambda: now[0])

    assert [limiter.check("me", "chat") for _ in range(3)] == [None, None, None]
    assert limiter.check("me", "chat") == 60.0  # the 4th within a minute must wait
    assert limiter.check("someone-else", "chat") is None  # counted per client
    assert limiter.check("me", "read") is None  # and per kind of request

    now[0] = 61.0
    assert limiter.check("me", "chat") is None  # a minute later: allowed again


def test_rate_limit_groups():
    assert group_for("POST", "/chat") == "chat"
    assert group_for("POST", "/documents") == "upload"
    assert group_for("POST", "/voice/wake") == "voice"
    assert group_for("DELETE", "/memories/abc") == "write"
    assert group_for("GET", "/reminders") == "read"
    assert group_for("GET", "/app.js") is None  # the page's own files are never limited


async def test_flooding_the_chat_is_refused(client, fake_llm):
    transport: httpx.ASGITransport = client._transport
    transport.app.state.rate_limiter = RateLimiter({"chat": 2})
    fake_llm.script = ["one", "two", "never sent"]
    headers = {"Origin": "http://test"}

    first = await client.post("/chat", json={"message": "hi"}, headers=headers)
    second = await client.post("/chat", json={"message": "hi"}, headers=headers)
    third = await client.post("/chat", json={"message": "hi"}, headers=headers)

    assert (first.status_code, second.status_code, third.status_code) == (200, 200, 429)
    assert third.json()["error"]["type"] == "rate_limited"
    assert int(third.headers["Retry-After"]) >= 1
    assert len(fake_llm.script) == 1  # the model never ran for the refused request


# ---------- security headers ----------


async def test_security_headers_on_every_response(client):
    for path in ("/", "/health", "/does-not-exist"):
        headers = (await client.get(path)).headers
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert headers["X-Frame-Options"] == "DENY"
        assert headers["Referrer-Policy"] == "no-referrer"
        policy = headers["Content-Security-Policy"]
        assert "default-src 'self'" in policy
        assert "frame-ancestors 'none'" in policy
        assert "'unsafe-inline'" not in policy  # injected <script> or style can't run
        assert "'unsafe-eval'" not in policy


async def test_the_page_has_no_inline_scripts_or_styles(client):
    """The Content-Security-Policy forbids them, so the page itself must not need them."""
    html = (await client.get("/")).text
    assert "<script>" not in html
    assert "<style" not in html
    assert " style=" not in html
    assert "onclick=" not in html


def test_the_policy_allows_the_live_connection_on_an_extra_name():
    """Reached through Tailscale (https://<pc>.<tailnet>.ts.net) the page uses wss:// on that
    name; Safari doesn't always count it as 'self', so it is listed - and only that name."""
    from app.api.middleware import CONTENT_SECURITY_POLICY, content_security_policy

    assert content_security_policy(frozenset()) == CONTENT_SECURITY_POLICY
    policy = content_security_policy(frozenset({"pc.tail1234.ts.net"}))
    connect = next(p for p in policy.split("; ") if p.startswith("connect-src"))
    assert "wss://pc.tail1234.ts.net" in connect
    assert "wss:" not in connect.replace("wss://pc.tail1234.ts.net", "")  # no wildcard
    assert "script-src 'self' blob:" in policy  # nothing else loosened


async def test_the_header_uses_the_configured_names(client):
    client._transport.app.state.allowed_hosts = frozenset({"test", "testserver", "pc.ts.net"})
    response = await client.get("/")
    assert "wss://pc.ts.net" in response.headers["content-security-policy"]
