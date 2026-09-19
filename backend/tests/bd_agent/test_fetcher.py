from __future__ import annotations

import httpx
import pytest

from kerui_recruit.bd_agent.fetcher import JinaReaderFetcher


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://r.jina.ai",
    )


@pytest.mark.asyncio
async def test_fetch_returns_markdown() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/https://example.com/job"
        return httpx.Response(200, text="# Title\n\nbody")

    fetcher = JinaReaderFetcher(client=_client(handler))
    assert await fetcher.fetch("https://example.com/job") == "# Title\n\nbody"


@pytest.mark.asyncio
async def test_fetch_returns_none_on_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    fetcher = JinaReaderFetcher(client=_client(handler))
    assert await fetcher.fetch("https://example.com/job") is None
