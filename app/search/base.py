"""Web search: the provider interface and result format.

A *search provider* turns a query into a list of results (title, link,
snippet). Like the LLM layer, the rest of ARTHUR only knows this interface,
so DuckDuckGo can be swapped for SearXNG, Brave or Tavily in one place.
"""

from abc import ABC, abstractmethod
from urllib.parse import urlsplit

from pydantic import BaseModel, computed_field


class SearchResult(BaseModel):
    title: str
    url: str
    snippet: str = ""

    @computed_field
    @property
    def source(self) -> str:
        """The website, e.g. 'python.org' - handy for short citations."""
        host = urlsplit(self.url).hostname or ""
        return host.removeprefix("www.")


class SearchError(Exception):
    """A search failure that is safe to show the user."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class SearchProvider(ABC):
    name: str

    @abstractmethod
    async def search(self, query: str, max_results: int) -> list[SearchResult]:
        """Return up to max_results results. Raise SearchError on failure."""

    async def aclose(self) -> None:  # noqa: B027 - optional hook
        pass
