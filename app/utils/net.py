"""Small network helpers with no dependencies (so settings can use them)."""

from urllib.parse import urlsplit, urlunsplit


def prefer_ipv4_loopback(url: str) -> str:
    """ "http://localhost:11434" -> "http://127.0.0.1:11434".

    On Windows "localhost" is tried as IPv6 (::1) first. Ollama listens on IPv4 only, so
    every new connection waits for that attempt to fail: measured 2.05 s per connection
    with plain sockets (~0.3 s with async "happy eyeballs") against 1-15 ms for 127.0.0.1.
    A server that really listens on IPv6 only can be addressed as http://[::1]:port.
    """
    parts = urlsplit(url)
    if (parts.hostname or "").lower() != "localhost":
        return url
    netloc = "127.0.0.1" + (f":{parts.port}" if parts.port else "")
    return urlunsplit(parts._replace(netloc=netloc))
