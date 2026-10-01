"""Network safety rules shared by the API, web reading and the browser.

Two directions:

INCOMING (who may talk to ARTHUR)
    `host_allowed` - ARTHUR only answers requests addressed to localhost/127.0.0.1.
    Why: "DNS rebinding". A website can point its own name (evil.example) at
    127.0.0.1; your browser then sends its requests to ARTHUR, and the Origin
    check alone doesn't notice because Origin and Host both say evil.example.
    The Host header gives it away.

OUTGOING (where ARTHUR may connect)
    `resolve_public` - look the name up ONCE, check every address is public, and
    return the address to connect to. Connecting to exactly that address ("pinning")
    closes the gap where a name answers "public" for the check and "192.168.1.1"
    a moment later for the real connection.
"""

import asyncio
import ipaddress
import re
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit

LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


class UnsafeUrlError(Exception):
    """The URL points somewhere ARTHUR must not go."""


class LookupFailed(Exception):
    """The host name doesn't exist (safe to show the user)."""


def host_name(host_header: str | None) -> str:
    """ "LocalHost:8000" -> "localhost", "[::1]:8000" -> "::1"."""
    host = (host_header or "").strip().lower()
    if host.startswith("["):
        return host[1:].split("]")[0]
    return host.rsplit(":", 1)[0] if ":" in host else host


def host_allowed(host_header: str | None, extra: frozenset[str] = frozenset()) -> bool:
    return host_name(host_header) in LOCAL_HOSTS | extra


@dataclass(frozen=True)
class Target:
    """A checked destination: connect to `ip`, but speak to `host` (Host header, TLS name)."""

    scheme: str
    host: str
    ip: str
    port: int


# A host where EVERY dot-separated part is a number (decimal, octal or 0x-hexadecimal).
# Deliberately not "any hex-looking text": bbc.de, fb.cc and cafe.de are real names.
_NUMBER = r"(?:0x[0-9a-f]+|\d+)"
_NUMERIC_HOST = re.compile(rf"{_NUMBER}(?:\.{_NUMBER})*", re.IGNORECASE)


def _is_ip_address(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def is_public(ip: str) -> bool:
    address = ipaddress.ip_address(ip)
    if getattr(address, "ipv4_mapped", None):  # ::ffff:192.168.1.1 is still private
        address = address.ipv4_mapped
    return address.is_global and not address.is_multicast


async def resolve_public(url: str) -> Target:
    """Raise UnsafeUrlError unless the URL is http(s) and resolves ONLY to public addresses."""
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
    if _NUMERIC_HOST.fullmatch(host) and not _is_ip_address(host):
        # 2130706433, 0x7f.0.0.1, 017700000001 ... other ways to write an address such as
        # 127.0.0.1. Linux would read them as IPs, Windows tries a slow DNS lookup. No real
        # website is named like this, so refuse outright - the same on every system.
        raise UnsafeUrlError("Unusual numeric addresses are not allowed.")
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError as exc:
        raise UnsafeUrlError("The link has an invalid port.") from exc
    return await resolve_public_host(parts.scheme, host, port)


async def resolve_public_host(scheme: str, host: str, port: int) -> Target:
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise LookupFailed(f"Couldn't find the website '{host}'.") from exc
    addresses = [info[4][0] for info in infos]
    # ALL answers must be public: one private address among them is enough to refuse.
    if not addresses or not all(is_public(ip) for ip in addresses):
        raise UnsafeUrlError(
            f"'{host}' points to a private or local network address - blocked for safety."
        )
    ipv4 = [ip for ip in addresses if ":" not in ip]
    return Target(scheme=scheme, host=host, ip=(ipv4 or addresses)[0], port=port)
