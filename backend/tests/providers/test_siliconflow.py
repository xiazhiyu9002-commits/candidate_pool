from __future__ import annotations

import httpx
import pytest

from kerui_recruit.providers.siliconflow import (
    SiliconFlowEmbeddingProvider,
    SiliconFlowRerankerProvider,
)


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://test",
    )


@pytest.mark.asyncio
async def test_embedding_parses_sorted_vectors_and_dimension() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = __import__("json").loads(request.content)
        assert body["model"] == "BAAI/bge-m3"
        assert request.url.path.endswith("/embeddings")
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": 1, "embedding": [2.0, 2.0]},
                    {"index": 0, "embedding": [1.0, 1.0]},
                ]
            },
        )

    provider = SiliconFlowEmbeddingProvider(
        api_key="test-key", client=_client(handler)
    )
    vectors = await provider.embed_documents(["a", "b"])

    assert vectors == [[1.0, 1.0], [2.0, 2.0]]
    assert provider.dimension == 1024


@pytest.mark.asyncio
async def test_reranker_orders_by_relevance_score() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/rerank")
        return httpx.Response(
            200,
            json={
                "results": [
                    {"index": 0, "relevance_score": 0.1},
                    {"index": 1, "relevance_score": 0.9},
                    {"index": 2, "relevance_score": 0.5},
                ]
            },
        )

    provider = SiliconFlowRerankerProvider(
        api_key="test-key", client=_client(handler)
    )
    order = await provider.rerank("q", ["a", "b", "c"])

    assert order == [1, 2, 0]


@pytest.mark.asyncio
async def test_embedding_maps_http_error_to_provider_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": "rate limited"})

    provider = SiliconFlowEmbeddingProvider(
        api_key="test-key", client=_client(handler), retry_attempts=1
    )
    with pytest.raises(Exception) as caught:
        await provider.embed_query("hello")

    assert caught.value.code == "E_API_RATE_LIMIT"
    assert caught.value.retryable is True


@pytest.mark.asyncio
async def test_embedding_retries_rate_limit_then_succeeds() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, json={"error": "rate limited"})
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0, 2.0]}]})

    provider = SiliconFlowEmbeddingProvider(
        api_key="test-key", client=_client(handler), retry_base_delay=0.01
    )
    vector = await provider.embed_query("hello")

    assert len(calls) == 2
    assert vector == [1.0, 2.0]


@pytest.mark.asyncio
async def test_reranker_exhausts_retries_and_surfaces_request_id() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(
            429,
            json={"error": "rate limited"},
            headers={"x-request-id": "req-1", "retry-after": "0.01"},
        )

    provider = SiliconFlowRerankerProvider(
        api_key="test-key",
        client=_client(handler),
        retry_attempts=2,
        retry_base_delay=0.01,
    )
    with pytest.raises(Exception) as caught:
        await provider.rerank_scored("q", ["a"])

    assert len(calls) == 2
    assert caught.value.code == "E_API_RATE_LIMIT"
    assert caught.value.retryable is True
    assert caught.value.request_id == "req-1"
    assert caught.value.retry_after_seconds == 0.01


@pytest.mark.asyncio
async def test_embedding_does_not_retry_non_retryable_status() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(400, json={"error": "bad request"})

    provider = SiliconFlowEmbeddingProvider(
        api_key="test-key", client=_client(handler), retry_base_delay=0.01
    )
    with pytest.raises(Exception) as caught:
        await provider.embed_query("hello")

    assert len(calls) == 1
    assert caught.value.code == "E_API_FORMAT"


@pytest.mark.asyncio
async def test_fallback_channel_takes_second_half_of_retries() -> None:
    """备份通道吃同一重试序列的后半段：请求数与等待时间都不增加。"""
    models: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        models.append(__import__("json").loads(request.content)["model"])
        return httpx.Response(429, json={"error": "rate limited"})

    provider = SiliconFlowRerankerProvider(
        api_key="test-key",
        client=_client(handler),
        model="BAAI/bge-reranker-v2-m3",
        fallback_models=("Pro/BAAI/bge-reranker-v2-m3",),
        retry_attempts=4,
        retry_base_delay=0.01,
    )
    with pytest.raises(Exception) as caught:
        await provider.rerank_scored("q", ["a"])

    assert models == ["BAAI/bge-reranker-v2-m3"] * 2 + ["Pro/BAAI/bge-reranker-v2-m3"] * 2
    assert caught.value.code == "E_API_RATE_LIMIT"


@pytest.mark.asyncio
async def test_fallback_channel_rescues_after_primary_share_exhausted() -> None:
    """主通道吃满自己的份额仍失败后，备份通道接管并成功返回。"""
    models: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        model = __import__("json").loads(request.content)["model"]
        models.append(model)
        if model.startswith("Pro/"):
            return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0, 2.0]}]})
        return httpx.Response(429, json={"error": "rate limited"})

    provider = SiliconFlowEmbeddingProvider(
        api_key="test-key",
        client=_client(handler),
        model="BAAI/bge-m3",
        fallback_models=("Pro/BAAI/bge-m3",),
        retry_attempts=4,
        retry_base_delay=0.01,
    )
    vectors = await provider.embed_documents(["a"])

    assert models == ["BAAI/bge-m3", "BAAI/bge-m3", "Pro/BAAI/bge-m3"]
    assert vectors == [[1.0, 2.0]]


@pytest.mark.asyncio
async def test_without_fallback_every_retry_uses_primary_model() -> None:
    models: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        models.append(__import__("json").loads(request.content)["model"])
        return httpx.Response(429, json={"error": "rate limited"})

    provider = SiliconFlowEmbeddingProvider(
        api_key="test-key",
        client=_client(handler),
        model="BAAI/bge-m3",
        retry_attempts=3,
        retry_base_delay=0.01,
    )
    with pytest.raises(Exception):
        await provider.embed_query("hello")

    assert models == ["BAAI/bge-m3"] * 3


@pytest.mark.asyncio
async def test_non_retryable_error_does_not_switch_channel() -> None:
    """非重试类错误换通道也救不了，立即上抛、不消耗备份通道。"""
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(400, json={"error": "bad request"})

    provider = SiliconFlowRerankerProvider(
        api_key="test-key",
        client=_client(handler),
        model="BAAI/bge-reranker-v2-m3",
        fallback_models=("Pro/BAAI/bge-reranker-v2-m3",),
        retry_base_delay=0.01,
    )
    with pytest.raises(Exception) as caught:
        await provider.rerank_scored("q", ["a"])

    assert len(calls) == 1
    assert caught.value.code == "E_API_FORMAT"