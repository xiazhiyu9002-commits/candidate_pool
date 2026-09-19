from __future__ import annotations

from typing import Protocol

import httpx


class WebFetcher(Protocol):
    async def fetch(self, url: str) -> str | None: ...


class JinaReaderFetcher:
    """Fetch a webpage as markdown through Jina Reader.

    ``GET https://r.jina.ai/{url}`` converts any page to clean markdown text.
    Returns ``None`` on any error so callers can fall back to the search
    snippet instead of aborting the whole BD run.
    """

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        base_url: str = "https://r.jina.ai",
    ) -> None:
        self._client = client
        self.base_url = base_url.rstrip("/")

    async def fetch(self, url: str) -> str | None:
        try:
            client = self._client or httpx.AsyncClient(timeout=30.0)
            try:
                response = await client.get(f"{self.base_url}/{url}")
                response.raise_for_status()
                text = response.text.strip()
                return text or None
            finally:
                if self._client is None:
                    await client.aclose()
        except httpx.HTTPError:
            return None
