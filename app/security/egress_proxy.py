"""A tiny local proxy that every connection of ARTHUR's browser must pass through.

Why: Chromium looks up host names itself, so a check done by ARTHUR beforehand can be
tricked ("DNS rebinding": the name answers with a public address for the check and with
192.168.1.1 for the real connection). A browser that uses a proxy does NOT look names
up - it asks the proxy to connect. So this proxy does the lookup, checks that the
address is public, and connects to exactly that address.

    Chromium --"CONNECT example.com:443"--> EgressProxy --(checked IP)--> the website

HTTPS stays encrypted end to end: after "200 Connection established" the proxy only
copies bytes in both directions and cannot read them. Plain http requests are forwarded
with "Connection: close", so each request is checked on its own.
"""

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

from app.observability.logging import get_logger
from app.security.network import LookupFailed, Target, UnsafeUrlError, resolve_public_host

log = get_logger(__name__)

HostResolver = Callable[[str, str, int], Awaitable[Target]]  # (scheme, host, port) -> Target

MAX_HEAD_BYTES = 64 * 1024
CONNECT_TIMEOUT = 10.0
HEAD_TIMEOUT = 10.0


def port_allowed(port: int) -> bool:
    return port in (80, 443) or 1024 <= port <= 65535  # no mail, SSH, file-sharing ports


class EgressProxy:
    def __init__(self, resolver: HostResolver = resolve_public_host) -> None:
        self.resolver = resolver
        self.blocked: list[str] = []  # "host:port" of refused connections (for tests/logs)
        self.connections = 0
        self.port: int | None = None
        self._server: asyncio.Server | None = None

    async def start(self) -> int:
        self._server = await asyncio.start_server(
            self._handle, host="127.0.0.1", port=0, limit=MAX_HEAD_BYTES
        )
        self.port = self._server.sockets[0].getsockname()[1]
        log.info("egress_proxy_started", port=self.port)
        return self.port

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
            self._server = None

    # ---------- one browser connection ----------

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), HEAD_TIMEOUT)
            request_line, *header_lines = head.decode("latin-1").split("\r\n")
            method, target, _version = request_line.split(" ", 2)
        except (TimeoutError, ValueError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            await _reply(writer, 400, "Bad request")
            return
        except (ConnectionError, OSError):
            return

        try:
            if method.upper() == "CONNECT":
                host, _, port_text = target.rpartition(":")
                scheme, host, port, path = "https", host.strip("[]"), int(port_text), ""
            else:
                parts = urlsplit(target)
                if parts.scheme != "http" or not parts.hostname:
                    raise UnsafeUrlError("Only http and https are allowed.")
                scheme, host, port = "http", parts.hostname, parts.port or 80
                path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
            if not port_allowed(port):
                raise UnsafeUrlError(f"Port {port} is not allowed.")
            checked = await self.resolver(scheme, host, port)
        except (UnsafeUrlError, LookupFailed, ValueError) as exc:
            self.blocked.append(target)
            log.warning("egress_blocked", target=target[:200], reason=str(exc))
            await _reply(writer, 403, "Blocked by ARTHUR")
            return

        try:
            upstream_reader, upstream_writer = await asyncio.wait_for(
                asyncio.open_connection(checked.ip, checked.port), CONNECT_TIMEOUT
            )  # the CHECKED address - the name is never looked up again
        except (TimeoutError, OSError):
            await _reply(writer, 502, "Could not connect")
            return

        self.connections += 1
        if method.upper() == "CONNECT":
            writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
        else:
            upstream_writer.write(_origin_request(method, path, header_lines))
        await asyncio.gather(
            _copy(reader, upstream_writer), _copy(upstream_reader, writer), return_exceptions=True
        )


def _origin_request(method: str, path: str, header_lines: list[str]) -> bytes:
    """Rewrite "GET http://host/path" (proxy form) to "GET /path" (what servers expect)."""
    skip = ("proxy-connection:", "connection:", "proxy-authorization:")
    headers = [h for h in header_lines if h and not h.lower().startswith(skip)]
    lines = [f"{method} {path} HTTP/1.1", *headers, "Connection: close", "", ""]
    return "\r\n".join(lines).encode("latin-1")


async def _reply(writer: asyncio.StreamWriter, status: int, reason: str) -> None:
    with contextlib.suppress(ConnectionError, OSError):
        body = reason.encode()
        writer.write(
            f"HTTP/1.1 {status} {reason}\r\nContent-Length: {len(body)}\r\n"
            "Content-Type: text/plain\r\nConnection: close\r\n\r\n".encode()
            + body
        )
        await writer.drain()
        writer.close()


async def _copy(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        with contextlib.suppress(ConnectionError, OSError):
            writer.close()
