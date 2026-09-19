from __future__ import annotations

import httpx

from kerui_recruit.bd_search.service import WebSearchProvider, WebSearchResult


class NullWebSearchProvider(WebSearchProvider):
    """Offline placeholder that returns no results.

    Replaced by a real provider (e.g. Tavily) when an API key is configured.
    """

    def search(self, query: str, limit: int = 10) -> list[WebSearchResult]:
        return []


# Low-value sources: news, media, blogs and portals rarely describe a single
# open role, so they pollute BD lead discovery. Excluding them biases results
# toward recruitment platforms and company career pages.
_LOW_QUALITY_DOMAINS = [
    "zhihu.com",
    "sohu.com",
    "sina.com.cn",
    "163.com",
    "qq.com",
    "ifeng.com",
    "thepaper.cn",
    "36kr.com",
    "leiphone.com",
    "ithome.com",
    "csdn.net",
    "juejin.cn",
    "cnblogs.com",
    "segmentfault.com",
    "medium.com",
    "gov.cn",
    "weixin.qq.com",
    "baijiahao.baidu.com",
]


class TavilyWebSearchProvider(WebSearchProvider):
    """Tavily search adapter for BD lead discovery.

    Uses the synchronous httpx client because the BD search service is
    synchronous. A client can be injected for tests.
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.tavily.com",
        client: httpx.Client | None = None,
        exclude_domains: list[str] | None = None,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self._client = client
        self.exclude_domains = (
            exclude_domains if exclude_domains is not None else list(_LOW_QUALITY_DOMAINS)
        )

    def search(self, query: str, limit: int = 10) -> list[WebSearchResult]:
        client = self._client or httpx.Client(timeout=40.0)
        request_json: dict = {
            "api_key": self.api_key,
            "query": query,
            "max_results": limit,
            "search_depth": "advanced",
            "include_raw_content": True,
        }
        if self.exclude_domains:
            request_json["exclude_domains"] = self.exclude_domains

        try:
            response = client.post(
                f"{self.base_url}/search",
                json=request_json,
            )
            response.raise_for_status()
            payload = response.json()
        finally:
            if self._client is None:
                client.close()

        results: list[WebSearchResult] = []
        for item in payload.get("results", []):
            results.append(
                WebSearchResult(
                    title=item.get("title", ""),
                    url=item.get("url", ""),
                    snippet=item.get("content", ""),
                    source="tavily",
                    raw_content=item.get("raw_content"),
                )
            )
        return results


class SerpApiWebSearchProvider(WebSearchProvider):
    """SerpApi search adapter (Google engine) for BD lead discovery.

    Drop-in alternative to Tavily, selected when a SerpApi key is configured.
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://serpapi.com",
        client: httpx.Client | None = None,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self._client = client

    def search(self, query: str, limit: int = 10) -> list[WebSearchResult]:
        client = self._client or httpx.Client(timeout=40.0)
        try:
            response = client.get(
                f"{self.base_url}/search.json",
                params={
                    "engine": "google",
                    "q": query,
                    "num": limit,
                    "api_key": self.api_key,
                },
            )
            response.raise_for_status()
            payload = response.json()
        finally:
            if self._client is None:
                client.close()

        results: list[WebSearchResult] = []
        for item in payload.get("organic_results", []):
            results.append(
                WebSearchResult(
                    title=item.get("title", ""),
                    url=item.get("link", ""),
                    snippet=item.get("snippet", ""),
                    source="serpapi",
                )
            )
        return results
