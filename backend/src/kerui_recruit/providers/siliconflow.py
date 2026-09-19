from __future__ import annotations

import asyncio
import logging

import httpx

from kerui_recruit.providers.errors import ProviderError, map_http_error

logger = logging.getLogger(__name__)

_RETRY_ATTEMPTS = 4  # 首次 + 最多 3 次重试；退避 1s→2s→4s，累计睡眠 7s
_RETRY_BASE_DELAY = 1.0  # 秒；实测 429 拒绝延迟 p50=0.30s，1s 起退避足够
_RETRY_MAX_DELAY = 6.0  # 退避上限：attempts=4 时实际序列 1s→2s→4s，也把上游 Retry-After 截断在 6s 内
_RETRY_STATUSES = frozenset({408, 429, 500, 502, 503, 504})


def _request_id(response: httpx.Response) -> str | None:
    for header in ("x-request-id", "x-sf-request-id"):
        value = response.headers.get(header)
        if value:
            return value
    return None


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after")
    if value is None:
        return None
    try:
        return max(0.0, float(value.strip()))
    except (AttributeError, ValueError):
        return None


def _attempt_models(primary: str | None, fallback_models: tuple[str, ...],
                    attempts: int) -> list[str | None]:
    """把总尝试次数分配给主模型与备份通道（主模型优先拿余数）。

    备份通道与主模型同 base_url、同 key、同 payload、同响应结构，只差 ``model`` 一个
    字段，因此把同一个重试序列的后半段交给它，**请求数与等待时间都不增加**，只是多了一次
    换通道的机会：实测主通道的 429 里约 89% 会在前两次尝试内恢复（继续重试主模型即可），
    剩下 11% 交给备份通道的全新配额池。attempts=4 + 1 个备份时分配为「主 2 / 备 2」。
    """
    models: list[str | None] = [primary]
    for model in fallback_models:
        if model and model != primary and model not in models:
            models.append(model)
    if len(models) == 1:
        return models * attempts
    share, extra = divmod(attempts, len(models))
    plan: list[str | None] = []
    for index, model in enumerate(models):
        plan.extend([model] * (share + (1 if index < extra else 0)))
    return plan


async def _post_with_retry(
    *,
    client: httpx.AsyncClient,
    url: str,
    api_key: str,
    payload: dict,
    service: str,
    attempts: int,
    base_delay: float,
    max_delay: float,
    fallback_models: tuple[str, ...] = (),
) -> httpx.Response:
    """带指数退避的 POST：只在可重试状态码/网络错误上重试，其余立即上抛。

    上游 request-id 与 Retry-After 透传进 ProviderError，供日志与调用方关联。
    ``fallback_models`` 给出备份通道：重试序列的后半段改为请求备份模型，用于绕过
    单个通道的抖动（实测上游偶发 429 且不返回 Retry-After，纯重试会耗尽）。
    """
    attempts = max(1, attempts)
    plan = _attempt_models(payload.get("model"), tuple(fallback_models), attempts)
    last: ProviderError | None = None
    for attempt, model in enumerate(plan):
        body = {**payload, "model": model} if model else payload
        try:
            response = await client.post(
                url, headers={"Authorization": f"Bearer {api_key}"}, json=body
            )
        except httpx.RequestError:
            last = ProviderError(
                code="E_API_NETWORK",
                retryable=True,
                user_message=f"无法连接 {service} 服务",
            )
        else:
            if response.status_code < 400:
                return response
            last = map_http_error(
                response.status_code,
                request_id=_request_id(response),
                retry_after_seconds=_retry_after(response),
            )
            if response.status_code not in _RETRY_STATUSES:
                raise last
        if attempt + 1 >= len(plan):
            break
        delay = min(
            max(base_delay * (2**attempt), last.retry_after_seconds or 0.0), max_delay
        )
        logger.warning(
            "%s retry attempt=%d/%d model=%s next_model=%s code=%s category=%s "
            "request_id=%s delay=%.2fs",
            service,
            attempt + 1,
            len(plan),
            model,
            plan[attempt + 1],
            last.code,
            last.category,
            last.request_id,
            delay,
        )
        await asyncio.sleep(delay)
    assert last is not None
    raise last


class SiliconFlowEmbeddingProvider:
    """BGE-M3 embeddings served through SiliconFlow's OpenAI-compatible API."""

    dimension = 1024

    def __init__(
        self,
        *,
        api_key: str,
        client: httpx.AsyncClient,
        base_url: str = "https://api.siliconflow.cn/v1",
        model: str = "BAAI/bge-m3",
        fallback_models: tuple[str, ...] = (),
        retry_attempts: int = _RETRY_ATTEMPTS,
        retry_base_delay: float = _RETRY_BASE_DELAY,
        retry_max_delay: float = _RETRY_MAX_DELAY,
    ) -> None:
        self.api_key = api_key
        self.client = client
        self.base_url = base_url.rstrip("/")
        self.model = model
        # 备份模型必须与主模型同一向量空间（同模型的不同通道），否则查询向量与索引
        # 里的向量不可比。实测 Pro/BAAI/bge-m3 与 BAAI/bge-m3 逐条 cos≈1.0000。
        self.fallback_models = tuple(fallback_models)
        self.retry_attempts = retry_attempts
        self.retry_base_delay = retry_base_delay
        self.retry_max_delay = retry_max_delay

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        vectors = await self._embed(texts)
        return vectors

    async def embed_query(self, text: str) -> list[float]:
        return (await self._embed([text]))[0]

    async def _embed(self, texts: list[str]) -> list[list[float]]:
        response = await _post_with_retry(
            client=self.client,
            url=f"{self.base_url}/embeddings",
            api_key=self.api_key,
            payload={
                "model": self.model,
                "input": texts,
                "encoding_format": "float",
            },
            service="Embedding",
            attempts=self.retry_attempts,
            base_delay=self.retry_base_delay,
            max_delay=self.retry_max_delay,
            fallback_models=self.fallback_models,
        )
        try:
            payload = response.json()
            data = sorted(payload["data"], key=lambda item: item["index"])
            return [item["embedding"] for item in data]
        except (KeyError, IndexError, TypeError, ValueError) as error:
            raise ProviderError(
                code="E_API_SCHEMA",
                retryable=True,
                user_message="Embedding 返回内容不符合结构要求",
            ) from error


class SiliconFlowRerankerProvider:
    """BGE-reranker-v2-m3 served through SiliconFlow's rerank endpoint."""

    def __init__(
        self,
        *,
        api_key: str,
        client: httpx.AsyncClient,
        base_url: str = "https://api.siliconflow.cn/v1",
        model: str = "BAAI/bge-reranker-v2-m3",
        fallback_models: tuple[str, ...] = (),
        retry_attempts: int = _RETRY_ATTEMPTS,
        retry_base_delay: float = _RETRY_BASE_DELAY,
        retry_max_delay: float = _RETRY_MAX_DELAY,
    ) -> None:
        self.api_key = api_key
        self.client = client
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.fallback_models = tuple(fallback_models)
        self.retry_attempts = retry_attempts
        self.retry_base_delay = retry_base_delay
        self.retry_max_delay = retry_max_delay

    async def rerank(self, query: str, documents: list[str]) -> list[int]:
        scored = await self.rerank_scored(query, documents)
        seen: set[int] = set()
        order: list[int] = []
        for index, _ in sorted(scored, key=lambda item: item[1], reverse=True):
            if index not in seen:
                seen.add(index)
                order.append(index)
        return order

    async def rerank_scored(self, query: str, documents: list[str]) -> list[tuple[int, float]]:
        """Return (index, relevance_score) pairs from the provider, preserving
        the model's actual relevance scores instead of a positional proxy."""
        if not documents:
            return []
        response = await _post_with_retry(
            client=self.client,
            url=f"{self.base_url}/rerank",
            api_key=self.api_key,
            payload={
                "model": self.model,
                "query": query,
                "documents": documents,
            },
            service="Reranker",
            attempts=self.retry_attempts,
            base_delay=self.retry_base_delay,
            max_delay=self.retry_max_delay,
            fallback_models=self.fallback_models,
        )
        try:
            payload = response.json()
            return [
                (item["index"], float(item.get("relevance_score", 0.0)))
                for item in payload["results"]
            ]
        except (KeyError, IndexError, TypeError, ValueError) as error:
            raise ProviderError(
                code="E_API_SCHEMA",
                retryable=True,
                user_message="Reranker 返回内容不符合结构要求",
            ) from error