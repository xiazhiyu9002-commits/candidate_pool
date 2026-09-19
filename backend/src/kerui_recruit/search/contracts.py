from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from kerui_recruit.search.degrees import DEGREE_ORDER, degrees_at_least


@dataclass(frozen=True, slots=True)
class SearchChunk:
    id: str
    candidate_id: str
    revision_id: str
    content: str
    vector: tuple[float, ...]
    total_years: float | None
    highest_degree: str | None
    location: str | None
    candidate_status: str
    qs_rank: int | None = None
    school_level: str | None = None
    preferred_location: str | None = None
    preferred_locations: tuple[str, ...] = ()
    keyword_text: str | None = None
    keyword_index_text: str | None = None
    vector_text: str | None = None
    body_index_text: str | None = None
    chunk_type: str = "parent"  # "parent" 整份简历 / "child" 单段经历或项目
    parent_id: str | None = None  # 子 chunk 指向父 chunk 的 revision_id
    kind: str = "parent"  # 子切片类型：parent / profile_point / experience / project
    sequence: int | None = None  # 子切片在同一父切片下的序号
    evidence_path: tuple[str, ...] = ()  # 子切片可追溯的结构化证据路径
    name_terms: tuple[str, ...] = ()
    school_terms: tuple[str, ...] = ()
    company_terms: tuple[str, ...] = ()
    title_terms: tuple[str, ...] = ()
    location_terms: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()
    age: int | None = None
    school_tags: tuple[str, ...] = ()
    direction: str | None = None
    school_region: str | None = None
    specializations: tuple[str, ...] = ()  # 细分职业专长标签（旧版枚举，仅存量兼容；新数据见 career_specializations）
    # 多值方向体系：职业大类（≤2）/ 职业细分（≤4）/ 业务方向（≤2），筛选为 OR 关系。
    career_directions: tuple[str, ...] = ()
    career_specializations: tuple[str, ...] = ()
    business_directions: tuple[str, ...] = ()

    @property
    def effective_keyword_text(self) -> str:
        """The text used for lexical (FTS) recall. Falls back to the legacy alias."""
        return self.keyword_text if self.keyword_text is not None else self.content

    @property
    def effective_keyword_index_text(self) -> str:
        """The word-level FTS text. Falls back to keyword_text for compatibility."""
        if self.keyword_index_text is not None:
            return self.keyword_index_text
        return self.effective_keyword_text

    @property
    def effective_vector_text(self) -> str:
        """The text embedded into the vector. Never reuses keyword text unless absent."""
        if self.vector_text is not None:
            return self.vector_text
        if self.keyword_text is not None:
            return self.keyword_text
        return self.content


# 学历层级：低 -> 高。用于「本科及以上」这类包含式条件（见 search/degrees.py）。
# DEGREE_ORDER 与 degrees_at_least 由 degrees.py 统一维护，这里仅转发。

# 学校等级的重叠关系：985 也是 211，211 也是双一流。
# 要求「211」时应命中 985 与 211；要求「双一流」时命中全部三档。
SCHOOL_LEVEL_EXPAND = {
    "985": ("985",),
    "211": ("211", "985"),
    "双一流": ("双一流", "211", "985"),
    "海外": ("海外",),
    "普通": ("普通",),
}


def school_levels_at_least(level: str | None) -> tuple[str, ...]:
    """Return the school-level values that satisfy a level condition."""
    if level is None:
        return ()
    return SCHOOL_LEVEL_EXPAND.get(level, (level,))


@dataclass(frozen=True, slots=True)
class CandidateFilters:
    min_years: float | None = None
    max_years: float | None = None
    min_age: int | None = None
    max_age: int | None = None
    highest_degree: str | None = None  # 语义：最低学历层级
    degree_exact: bool = False  # True 表示「仅该学历」精确限定
    location: str | None = None  # 单值现居地（向后兼容）
    locations: tuple[str, ...] = ()  # 多值现居地（或关系）
    preferred_location: str | None = None  # 求职意向地（单值，向后兼容）
    preferred_locations: tuple[str, ...] = ()  # 多值求职意向地（或关系）
    candidate_status: str | None = "AVAILABLE"
    max_qs_rank: int | None = None
    school_level: str | None = None  # 语义：最低学校等级层级
    exclude_skills: tuple[str, ...] = ()  # 排除技能（召回后硬过滤）
    phone: str | None = None  # 手机号（按规范化指纹在数据库层精确过滤）
    gender: str | None = None  # 性别（按 男/女 在数据库层精确过滤）
    name: str | None = None  # 姓名（字段内关键词匹配）
    company: str | None = None  # 公司（匹配 current_company 与全部工作经历的公司）
    title: str | None = None  # 职位（匹配 current_title 与全部工作经历的职位）
    school: str | None = None  # 学校（匹配学校/教育经历，含别名解析）
    direction: str | None = None  # 职业方向（技术岗粗分类，等值过滤）
    school_region: str | None = None  # 学校属地（domestic 国内 / overseas 国外）
    specializations: tuple[str, ...] = ()  # 旧版专长枚举，仅存量兼容
    career_directions: tuple[str, ...] = ()  # 职业方向大类（多选，OR）
    career_specializations: tuple[str, ...] = ()  # 职业方向细分（多选，OR）
    business_directions: tuple[str, ...] = ()  # 业务方向（多选，OR）
    candidate_ids: tuple[str, ...] = ()  # 预过滤候选人集合（手机号/性别下沉到数据库层后传入）

    def degree_values(self) -> tuple[str, ...]:
        if self.highest_degree is None:
            return ()
        if self.degree_exact:
            return (self.highest_degree,)
        return degrees_at_least(self.highest_degree)

    def school_level_values(self) -> tuple[str, ...]:
        return school_levels_at_least(self.school_level)

    def location_values(self) -> tuple[str, ...]:
        values = [self.location] if self.location else []
        values.extend(self.locations)
        return tuple(dict.fromkeys(v for v in values if v))

    def preferred_location_values(self) -> tuple[str, ...]:
        values = [self.preferred_location] if self.preferred_location else []
        values.extend(self.preferred_locations)
        return tuple(dict.fromkeys(v for v in values if v))


@dataclass(frozen=True, slots=True)
class SearchRequest:
    query: str
    query_vector: tuple[float, ...]
    filters: CandidateFilters
    limit: int = 20


@dataclass(frozen=True, slots=True)
class SearchHit:
    chunk_id: str
    candidate_id: str
    revision_id: str
    content: str
    score: float
    matched_channels: tuple[str, ...]
    total_years: float | None
    highest_degree: str | None
    location: str | None
    qs_rank: int | None = None
    rerank_score: float | None = None  # 重排分（0~1 或原始），与召回分分离
    verified_exclusions: tuple[str, ...] = ()  # Complete index evidence already checked during recall.
    vector_text: str = ""  # 语义检索文本（AI 画像 + 结构化证据），用于重排。


@dataclass(frozen=True, slots=True)
class QueryPlan:
    """搜索执行计划：供 API 回显改写状态与语义查询文本。"""

    operator: str
    rewrite_requested: bool
    rewrite_status: str  # "disabled" | "not_applicable" | "success" | "unavailable"
    semantic_query: str | None = None


@dataclass(frozen=True, slots=True)
class SearchPage:
    items: tuple[SearchHit, ...]
    degraded_reasons: tuple[str, ...] = ()
    # 区分空结果的根因：no_match（真的没有）/ index_not_ready / service_error
    empty_reason: str | None = None
    query_plan: QueryPlan | None = None


def resolve_search_status(
    items: tuple | list,
    empty_reason: str | None,
    degraded_reasons: tuple | list,
) -> str:
    """Unify search/match outcome into one of five statuses."""
    if items:
        return "degraded" if degraded_reasons else "success"
    if empty_reason == "index_not_ready":
        return "index_not_ready"
    if empty_reason == "service_error":
        return "service_error"
    if degraded_reasons:
        return "service_error"
    return "no_match"


class SearchIndex(Protocol):
    def upsert(self, chunks: list[SearchChunk]) -> None: ...

    def delete_revision(self, revision_id: str) -> None: ...

    def search(self, request: SearchRequest) -> list[SearchHit]: ...
