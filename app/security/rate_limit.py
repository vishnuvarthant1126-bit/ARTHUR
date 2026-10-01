"""Rate limiting: at most N requests per minute for each kind of request.

ARTHUR is a single-user local app, so this isn't about fairness between users. It
stops a runaway script, a looping web page or a bug from flooding the model, the
disk (uploads) or the microphone endpoints.

Method: a "sliding window" - remember the times of recent requests and count how
many fall inside the last 60 seconds.
"""

import time
from collections import defaultdict, deque
from collections.abc import Callable

WINDOW_SECONDS = 60.0

# group -> requests allowed per minute
DEFAULT_LIMITS = {
    "chat": 30,  # each one runs the language model
    "upload": 20,  # documents, images
    "voice": 240,  # hands-free mode sends a short clip whenever it hears sound
    "write": 120,  # other changes: memories, reminders, tool calls
    "read": 600,  # lists, health checks
}


def group_for(method: str, path: str) -> str | None:
    """Which limit applies to this request (None = not limited: the web page's own files)."""
    if method == "POST" and path == "/chat":
        return "chat"
    if method == "POST" and path.startswith(("/documents", "/vision")):
        return "upload"
    if path.startswith("/voice"):
        return "voice"
    if method not in ("GET", "HEAD", "OPTIONS"):
        return "write"
    api = ("/memories", "/documents", "/reminders", "/tools", "/audit", "/health", "/vision",
           "/metrics")  # fmt: skip
    return "read" if path.startswith(api) else None


class RateLimiter:
    def __init__(
        self,
        limits: dict[str, int] | None = None,
        *,
        enabled: bool = True,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.limits = {**DEFAULT_LIMITS, **(limits or {})}
        self.enabled = enabled
        self.clock = clock
        self._hits: dict[tuple[str, str], deque[float]] = defaultdict(deque)

    def check(self, client: str, group: str) -> float | None:
        """Count one request. Returns None if allowed, else seconds to wait."""
        limit = self.limits.get(group)
        if not self.enabled or limit is None:
            return None
        now = self.clock()
        hits = self._hits[(client, group)]
        while hits and now - hits[0] >= WINDOW_SECONDS:
            hits.popleft()  # older than a minute: no longer counts
        if len(hits) >= limit:
            return max(WINDOW_SECONDS - (now - hits[0]), 1.0)
        hits.append(now)
        return None
