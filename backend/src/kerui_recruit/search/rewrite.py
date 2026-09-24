"""有界且带缓存的语义查询改写器。

- 输入是 ``parse_query`` 剥离硬条件后的关键词文本。
- 定位是**保真规范化**（同义词对齐、缩写展开），不是岗位知识联想。
- 输出 JSON ``{"semantic_query": "..."}``；任何失败都回退原查询。
- 内部 outcome 为 ``changed / unchanged / rejected / unavailable``，由服务层收敛成
  公开四值 ``rewrite_status`` 与 ``rewrite_applied`` / ``rewrite_fallback_reason``。
- 缓存键 = 模型识别 + REWRITE_PROMPT_VERSION + LEXICON_VERSION + 规范化原关键词，
  LRU 上限 256、TTL 600 秒。
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel, Field

from kerui_recruit.search.lexicon import (
    LEXICON_VERSION,
    concepts_from_query,
    is_curated_concept,
)
from kerui_recruit.search.query import parse_query

# 提示词版本：独立于 LEXICON_VERSION，避免提示词升级后继续命中旧缓存。
REWRITE_PROMPT_VERSION = "2"

# 输出长度上限：长查询最多 80 字符；原词不足 10 字符时最多 20 字符。
_REWRITE_MAX_CHARS = 80
_REWRITE_MAX_CHARS_SHORT = 20
_REWRITE_SHORT_LENGTH = 10
# schema 只挡住明显失控的输出；正常超长交给校验判为 rejected 而不是解析失败。
_REWRITE_PAYLOAD_MAX_CHARS = 1000


class SemanticRewritePayload(BaseModel):
    semantic_query: str = Field(max_length=_REWRITE_PAYLOAD_MAX_CHARS)


@dataclass(frozen=True, slots=True)
class RewriteResult:
    query: str
    # "changed"（采用了改写）| "unchanged"（无需改写）| "rejected"（校验拒绝）| "unavailable"
    outcome: str


class QueryRewriter(Protocol):
    async def rewrite(self, keywords: str) -> RewriteResult: ...


_SYSTEM_PROMPT = """你是招聘人才搜索查询的保真规范化器，不是联想扩写器。

任务：在不改变原查询意图、范围和强弱关系的前提下，生成一条简洁的语义检索查询，供向量模型使用。

规则：
1. 必须保留原查询中的每个岗位、技能、行业和业务概念。
2. 只允许规范明确且无歧义的缩写或别名，例如 JS→JavaScript、K8s→Kubernetes、Golang→Go、数仓→数据仓库。
3. 每个概念最多保留一个标准名和一个常用别名。
4. 禁止根据岗位名称推断并新增技能、框架、职责、行业、资历或业务场景。
5. 禁止添加城市、学历、年限、学校、公司、薪资、年龄、性别等条件。
6. 如果原查询已经清楚，原样返回。
7. 不要输出 OR、解释、括号说明、分类标签或关键词清单。
8. 输出不得超过原查询长度的 2 倍，且最多 80 个字符。
9. 只返回 JSON：{"semantic_query":"..."}

示例：
“JS 后端” → “JavaScript 后端开发”
“K8s 微服务” → “Kubernetes 微服务”
“数仓 TL” → “数据仓库 技术负责人 TL”
“核心交易系统 开发” → “核心交易系统 开发”
“软件工程师 后端” → “软件工程师 后端开发”
"""

# 改写文本经 parse_query 后可能引入的硬筛选字段。
_HARD_FIELDS = (
    "min_years", "max_years", "highest_degree", "location", "locations",
    "preferred_location", "preferred_locations", "max_qs_rank",
    "school_level", "exclude_skills",
)

# 列表式输出的标记：括号说明、分类标签，以及独立的 OR / AND。
_LIST_LABELS = ("同义词", "技能术语", "关键词清单", "分类标签")
_BRACKETS = ("(", ")", "（", "）", "[", "]", "【", "】", "{", "}")
_LOGIC_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9])(?:or|and)(?![A-Za-z0-9])", re.IGNORECASE)


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


def _normalize(text: str) -> str:
    return " ".join(text.split()).casefold()


def _concept_canonicals(text: str) -> set[str]:
    return {concept.canonical for concept in concepts_from_query(text)}


def _length_ok(original: str, rewritten: str) -> bool:
    if not rewritten.strip():
        return False
    if len(original) < _REWRITE_SHORT_LENGTH:
        return len(rewritten) <= _REWRITE_MAX_CHARS_SHORT
    return len(rewritten) <= min(_REWRITE_MAX_CHARS, len(original) * 2)


def _introduces_list_marker(original: str, rewritten: str) -> bool:
    """只拒绝**新增**的列表标记：原查询里本来就有的括号/连词不算改写引入。"""
    for marker in (*_BRACKETS, *_LIST_LABELS):
        if marker in rewritten and marker not in original:
            return True
    return (_LOGIC_TOKEN_RE.search(rewritten) is not None
            and _LOGIC_TOKEN_RE.search(original) is None)


def _concepts_preserved(original: str, rewritten: str) -> bool:
    """原查询的已策展概念必须全部保留，且不得新增原查询没有的已策展技能概念。

    标准名与同组别名经 canonical 化后视为同一概念。
    """
    original_concepts = _concept_canonicals(original)
    rewritten_concepts = _concept_canonicals(rewritten)
    curated = {canonical for canonical in original_concepts if is_curated_concept(canonical)}
    if not curated <= rewritten_concepts:
        return False
    added = {canonical for canonical in rewritten_concepts if is_curated_concept(canonical)} - curated
    return not added


def semantic_query_is_valid(original: str, rewritten: str) -> bool:
    """语义查询校验（改写与 LLM 解析共用同一套口径）。

    长度上限、不得新增列表标记、不得引入原查询没有的硬条件、已策展概念不得增删。
    任一不过即拒绝，调用方应静默回退原查询。
    """
    if not _length_ok(original, rewritten):
        return False
    if _introduces_list_marker(original, rewritten):
        return False
    if _introduces_hard_filter(parse_query(original).filters, parse_query(rewritten).filters):
        return False
    return _concepts_preserved(original, rewritten)


class SemanticQueryRewriter:
    def __init__(self, client, *, cache_size: int = 256, ttl_seconds: float = 600.0) -> None:
        self._client = client
        self._cache_size = cache_size
        self._ttl_seconds = ttl_seconds
        self._cache: dict[str, tuple[float, RewriteResult]] = {}

    def _cache_key(self, keywords: str) -> str:
        normalized = _normalize(keywords)
        identity = getattr(self._client, "cache_identity", None) or getattr(self._client, "model", "unspecified")
        return f"{identity}|{REWRITE_PROMPT_VERSION}|{LEXICON_VERSION}|{normalized}"

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
            # 模型、网络、超时、JSON/schema 解析失败：改写不可用。
            return self._store(key, RewriteResult(original, "unavailable"))

        if not _length_ok(original, semantic_query) or _introduces_list_marker(original, semantic_query):
            return self._store(key, RewriteResult(original, "rejected"))
        try:
            if _introduces_hard_filter(parse_query(original).filters, parse_query(semantic_query).filters):
                return self._store(key, RewriteResult(original, "rejected"))
        except Exception:
            return self._store(key, RewriteResult(original, "unavailable"))
        if not _concepts_preserved(original, semantic_query):
            return self._store(key, RewriteResult(original, "rejected"))
        if _normalize(semantic_query) == _normalize(original):
            return self._store(key, RewriteResult(original, "unchanged"))
        return self._store(key, RewriteResult(semantic_query, "changed"))
