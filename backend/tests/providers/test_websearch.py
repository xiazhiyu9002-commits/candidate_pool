from __future__ import annotations

import httpx

from kerui_recruit.providers.websearch import SerpApiWebSearchProvider, TavilyWebSearchProvider


class _FakeResponse:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {
            "results": [
                {
                    "title": "字节跳动科技有限公司 — Java 高级工程师",
                    "url": "https://example.com/1",
                    "content": "字节跳动科技有限公司招聘 Java 高级工程师，北京...",
                    "raw_content": "字节跳动科技有限公司 高级 Java 开发工程师 岗位要求...",
                },
                {
                    "title": "腾讯科技（深圳）有限公司",
                    "url": "https://example.com/2",
                    "content": "腾讯科技（深圳）有限公司 前端开发工程师 深圳...",
                },
            ]
        }


class _FakeClient:
    def __init__(self) -> None:
        self.captured_url: str | None = None
        self.captured_json: dict | None = None

    def post(self, url: str, *, json: dict) -> _FakeResponse:
        self.captured_url = url
        self.captured_json = json
        return _FakeResponse()

    def close(self) -> None:
        return None


def test_tavily_provider_maps_results() -> None:
    client = _FakeClient()
    provider = TavilyWebSearchProvider(api_key="test-key", client=client)  # type: ignore[arg-type]

    results = provider.search("Java 工程师 招聘", limit=5)

    assert len(results) == 2
    assert results[0].source == "tavily"
    assert results[0].title == "字节跳动科技有限公司 — Java 高级工程师"
    assert results[0].url == "https://example.com/1"
    assert results[0].snippet == "字节跳动科技有限公司招聘 Java 高级工程师，北京..."
    assert results[0].raw_content == "字节跳动科技有限公司 高级 Java 开发工程师 岗位要求..."
    # Second result has no raw_content, so it should fall back to None.
    assert results[1].raw_content is None

    assert client.captured_url == "https://api.tavily.com/search"
    assert client.captured_json == {
        "api_key": "test-key",
        "query": "Java 工程师 招聘",
        "max_results": 5,
        "search_depth": "advanced",
        "include_raw_content": True,
        "exclude_domains": [
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
        ],
    }


def test_tavily_provider_handles_empty_results() -> None:
    class EmptyResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"results": []}

    class EmptyClient:
        def post(self, url: str, *, json: dict) -> EmptyResponse:
            return EmptyResponse()

        def close(self) -> None:
            return None

    provider = TavilyWebSearchProvider(api_key="test-key", client=EmptyClient())  # type: ignore[arg-type]
    assert provider.search("no results") == []


class _SerpApiResponse:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {
            "organic_results": [
                {
                    "title": "字节跳动科技有限公司 — Java 高级工程师",
                    "link": "https://example.com/1",
                    "snippet": "字节跳动科技有限公司招聘 Java 高级工程师...",
                }
            ]
        }


class _SerpApiClient:
    def __init__(self) -> None:
        self.captured_url: str | None = None
        self.captured_params: dict | None = None

    def get(self, url: str, *, params: dict) -> _SerpApiResponse:
        self.captured_url = url
        self.captured_params = params
        return _SerpApiResponse()

    def close(self) -> None:
        return None


def test_serpapi_provider_maps_results() -> None:
    client = _SerpApiClient()
    provider = SerpApiWebSearchProvider(api_key="serp-key", client=client)  # type: ignore[arg-type]

    results = provider.search("Java 工程师 招聘", limit=5)

    assert len(results) == 1
    assert results[0].source == "serpapi"
    assert results[0].title == "字节跳动科技有限公司 — Java 高级工程师"
    assert results[0].url == "https://example.com/1"
    assert results[0].snippet == "字节跳动科技有限公司招聘 Java 高级工程师..."

    assert client.captured_url == "https://serpapi.com/search.json"
    assert client.captured_params == {
        "engine": "google",
        "q": "Java 工程师 招聘",
        "num": 5,
        "api_key": "serp-key",
    }
