"""有界且带缓存的语义查询改写器。

- 输入是 ``parse_query`` 剥离硬条件后的关键词文本。
- 输出 JSON ``{"semantic_query": "..."}``，非空且最长 500 字符。
- 任何失败（超时、无 LLM、结构错误、输出越界、新增硬筛选）都回退到原查询并标记 unavailable。
- 缓存键 = 模型识别 + LEXICON_VERSION + 规范化原关键词，LRU 上限 256、TTL 600 秒。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel, Field

from kerui_recruit.search.lexicon import LEXICON_VERSION
from kerui_recruit.search.query import parse_query


class SemanticRewritePayload(BaseModel):
    semantic_query: str = Field(min_length=1, max_length=500)


@dataclass(frozen=True, slots=True)
class RewriteResult:
    query: str
    status: str  # "success" | "unavailable"


class QueryRewriter(Protocol):
    async def rewrite(self, keywords: str) -> RewriteResult: ...


_SYSTEM_PROMPT = (
    "你是候选人搜索查询的语义改写器。只做同义词对齐与简历术语规范化，"
    "不得新增输入中不存在的任何硬性条件（如城市、学历、工作年限、学校、雇主、"
    "薪资、年龄、性别等）。只返回 JSON，格式为 {\"semantic_query\": \"...\"}。"
)

# 改写文本经 parse_query 后可能引入的硬筛选字段。
_HARD_FIELDS = (
    "min_years", "max_years", "highest_degree", "location", "locations",
    "preferred_location", "preferred_locations", "max_qs_rank",
    "school_level", "exclude_skills",
)


def _nondefault(value) -> bool:
    if value is None:
        return False
    if isinstance(value, (tuple, list, str)) and len(value) == 0:
        return False
    return True


def _introduces_hard_filter(original, rewritten) -> bool:
    """改写后是否引入了原查询不存在的硬筛选。"""
    for field in _HARD_FIELDS:
        new_value = getattr(rewritten, field)
        if _nondefault(new_value) and new_value != getattr(original, field):
            return True
    return False


class SemanticQueryRewriter:
    def __init__(self, client, *, cache_size: int = 256, ttl_seconds: float = 600.0) -> None:
        self._client = client
        self._cache_size = cache_size
        self._ttl_seconds = ttl_seconds
        self._cache: dict[str, tuple[float, RewriteResult]] = {}

    def _cache_key(self, keywords: str) -> str:
        normalized = " ".join(keywords.split()).casefold()
        identity = getattr(self._client, "cache_identity", None) or getattr(self._client, "model", "unspecified")
        return f"{identity}|{LEXICON_VERSION}|{normalized}"

    def _store(self, key: str, result: RewriteResult) -> RewriteResult:
        self._cache[key] = (time.monotonic(), result)
        while len(self._cache) > self._cache_size:
            self._cache.pop(next(iter(self._cache)))
        return result

    async def rewrite(self, keywords: str, *, deadline_monotonic: float | None = None) -> RewriteResult:
        original = keywords
        if not original.strip():
            return RewriteResult(original, "unavailable")
        key = self._cache_key(original)
        cached = self._cache.get(key)
        if cached is not None:
            timestamp, result = cached
            if time.monotonic() - timestamp < self._ttl_seconds:
                # 命中后移到末尾，保持 LRU 顺序。
                self._cache.pop(key, None)
                self._cache[key] = (timestamp, result)
                return result
            self._cache.pop(key, None)

        try:
            payload = await self._client.complete_json(
                [
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": original},
                ],
                SemanticRewritePayload,
                deadline_monotonic=deadline_monotonic,
            )
            semantic_query = payload.semantic_query.strip()
        except Exception:
            return self._store(key, RewriteResult(original, "unavailable"))

        if not semantic_query:
            return self._store(key, RewriteResult(original, "unavailable"))
        try:
            if _introduces_hard_filter(parse_query(original).filters, parse_query(semantic_query).filters):
                return self._store(key, RewriteResult(original, "unavailable"))
        except Exception:
            return self._store(key, RewriteResult(original, "unavailable"))
        return self._store(key, RewriteResult(semantic_query, "success"))
