"""解析质量抽检的确定性校验与汇总（阶段 3 验收的执行口径）。

设计原则：**抽检本身不能成为第二个不可信来源**，所以只做能机械核对的检查，
不引入任何模型判断。三类检查：

1. 原文依据（span）：姓名 / 手机号 / 性别 / 公司 / 职位 / 学校这类实体条件的值，
   必须能逐字定位回查询原文；手机号还必须是原文里的完整 11 位。
   —— LLM 幻觉出原文没有的实体在这里被抓住。
2. 词表依据：城市条件必须在城市词典内；方向枚举条件必须能被
   ``direction_codes_from_text`` 反查印证（即 A+C 交叉验证里的 C 侧）。
   —— 枚举码不在白名单、或查询里根本没有对应词，计误判。
3. 硬条件执行：对检索返回的候选人逐条复核已下推的硬条件，统计违反数。
   属性未知时记为「不可判」而不是违反，避免把缺失当错误。

本模块是纯函数集合，可直接单测；驱动脚本见 ``scripts/parse_quality_*.py``。
"""
from __future__ import annotations

import re
import statistics
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

from kerui_recruit.direction.classifier import direction_codes_from_text
from kerui_recruit.direction.policy import (
    BUSINESS_DIRECTION_LABELS,
    CAREER_DIRECTION_LABELS,
    SPECIALIZATION_LABELS,
)
from kerui_recruit.duplicates.service import normalize_phone
from kerui_recruit.search.cities import CITY_NAMES
from kerui_recruit.search.contracts import CandidateFilters
from kerui_recruit.search.degrees import normalize_degree
from kerui_recruit.search.query import has_skill

# 方向 code -> 中文标签：C 侧（分类器词表）反查不到时，查询里出现**精确标签**也算有依据，
# 与 ``search/parse.py`` 的逐字段校验口径一致。
DIRECTION_LABELS_BY_CODE = {
    **CAREER_DIRECTION_LABELS, **SPECIALIZATION_LABELS, **BUSINESS_DIRECTION_LABELS,
}

# 需要「原文可逐字定位」的实体条件（回显字段名 -> 校验口径）。
SPAN_FIELDS = frozenset({"name", "phone", "gender", "company", "companies", "title", "school"})
# 列表型条件：回显时被「、」拼成一串，校验前必须拆开，否则整串比对必然误判。
LIST_CONDITION_FIELDS = frozenset({
    "locations", "preferred_locations", "exclude_skills", "companies", "evidence_terms",
    "career_directions", "career_specializations", "business_directions",
})
DIRECTION_FIELDS = frozenset({
    "career_directions", "career_specializations", "business_directions",
})
CITY_FIELDS = frozenset({"locations", "preferred_locations"})
# 规则解析产物用单值字段名（location / preferred_location），回显与下推用多值名：
# 校验前统一成多值名，否则城市检查会被整条跳过。
CANONICAL_FIELDS = {"location": "locations", "preferred_location": "preferred_locations"}
_CITY_SET = frozenset(CITY_NAMES)
_CN_PHONE_LEN = 11


@dataclass(frozen=True, slots=True)
class Violation:
    """一条可机械复核的违规：kind 是判定类别，field/value 指向具体条件。"""

    kind: str
    field: str
    value: str
    detail: str = ""


def condition_values(field: str, value: str) -> tuple[str, ...]:
    """把一个回显条件的值拆成条目：列表型按顿号/逗号/竖线/斜杠/空格拆分。"""
    text = str(value or "").strip()
    if not text:
        return ()
    if field in LIST_CONDITION_FIELDS:
        return tuple(part for part in re.split(r"[、,，|/ ]+", text) if part)
    return (text,)


def _squash(text: str) -> str:
    """去掉空白并 casefold：与 ``search/parse.py:_has_span`` 的定位口径一致。"""
    return re.sub(r"\s+", "", str(text or "")).casefold()


def audit_conditions(query: str, conditions: Iterable[tuple[str, str]]) -> list[Violation]:
    """对一条查询的最终生效条件做三类确定性核对。

    ``conditions`` 是 ``(field, value)`` 序列（来自 ``effective_conditions`` 或解析产物）。
    原文依据按「忽略空白与大小写可定位」判定，与 ``parse.py:_has_span`` 同一口径；
    否则「数仓技术 TL」这类原文本来带空格的写法会被误报成幻觉。
    """
    squashed = _squash(query)
    digits = re.sub(r"\D", "", str(query or ""))
    violations: list[Violation] = []
    for raw_field, raw in conditions:
        field = CANONICAL_FIELDS.get(raw_field, raw_field)
        for value in condition_values(field, raw):
            if field == "phone":
                normalized = normalize_phone(value)
                if not normalized or len(normalized) != _CN_PHONE_LEN or normalized not in digits:
                    violations.append(Violation(
                        "phone_not_verbatim", field, value,
                        f"原文数字={len(digits)}位" if digits else "原文无数字",
                    ))
            elif field in SPAN_FIELDS:
                if _squash(value) not in squashed:
                    violations.append(Violation("span_missing", field, value))
            elif field in CITY_FIELDS:
                if value not in _CITY_SET:
                    violations.append(Violation("city_unknown", field, value))
            elif field in DIRECTION_FIELDS:
                if not _direction_has_evidence(query, value):
                    violations.append(Violation("direction_unverified", field, value))
    return violations


def _direction_has_evidence(query: str, code: str) -> bool:
    """方向条件是否有原文依据：分类器词表反查命中，或**原文**里出现该 code 的精确标签。

    与 ``search/parse.py:_validated_directions`` 同一口径（忽略空白与大小写）。
    """
    if code in direction_codes_from_text(query):
        return True
    squashed = _squash(query)
    for token in (DIRECTION_LABELS_BY_CODE.get(code), code):
        if token and _squash(token) in squashed:
            return True
    return False


@dataclass(frozen=True, slots=True)
class CandidateAttrs:
    """候选人侧复核硬条件所需的属性；``None`` / 空表示未知（不判违反）。

    字段口径与索引父行一致：``*_text`` 是索引里已拼好的空格分隔词项文本，
    ``school_tags`` 含学校等级。文本类条件在索引侧是**子串匹配**（``LIKE``），
    因此这里也按子串比对，避免探针比生产更严而报假违规。
    """

    total_years: float | None = None
    highest_degree: str | None = None
    age: int | None = None
    qs_rank: int | None = None
    school_region: str | None = None
    locations: tuple[str, ...] = ()
    preferred_locations: tuple[str, ...] = ()
    school_tags: tuple[str, ...] = ()
    name_text: str = ""
    company_text: str = ""
    title_text: str = ""
    school_text: str = ""
    direction_values: tuple[str, ...] = ()
    skills_text: str = ""

    @classmethod
    def from_index_row(cls, row: Mapping[str, object]) -> "CandidateAttrs":
        """从索引父行（或等价的数据库投影）构造：列名与 ``documents.build_candidate_document`` 一致。"""
        def nums(name: str) -> tuple[str, ...]:
            value = row.get(name)
            if isinstance(value, (list, tuple)):
                return tuple(str(v) for v in value if v)
            return (str(value),) if value else ()

        def number(name: str) -> float | None:
            value = row.get(name)
            return float(value) if isinstance(value, (int, float)) else None

        def joined(name: str, terms: str) -> str:
            text = row.get(name)
            return str(text) if text else " ".join(term.casefold() for term in nums(terms))

        directions = (*nums("career_directions"), *nums("career_specializations"),
                      *nums("business_directions"), *nums("direction"))
        return cls(
            total_years=number("total_years"),
            highest_degree=str(row.get("highest_degree") or "") or None,
            age=int(row["age"]) if isinstance(row.get("age"), int) else None,
            qs_rank=int(row["qs_rank"]) if isinstance(row.get("qs_rank"), int) else None,
            school_region=str(row.get("school_region") or "") or None,
            # location_terms 是归一化后的城市；缺少该列时退回原始 location。
            locations=nums("location_terms") or nums("location"),
            preferred_locations=(*nums("preferred_locations"), *nums("preferred_location")),
            school_tags=(*nums("school_tags"), *nums("school_level")),
            name_text=joined("name_text", "name_terms"),
            company_text=joined("company_text", "company_terms"),
            title_text=joined("title_text", "title_terms"),
            school_text=joined("school_text", "school_terms"),
            direction_values=directions,
            skills_text=str(row.get("keyword_index_text") or ""),
        )


def _intersects(left: Sequence[str], right: Iterable[str]) -> bool:
    folded = {str(value).casefold() for value in left if value}
    return any(str(value).casefold() in folded for value in right if value)


def _contains(text: str, value: str) -> bool:
    """镜像索引侧 ``LIKE '%value%'`` 的子串语义（列是已 casefold 的空格分隔词项文本）。"""
    return bool(text) and str(value).casefold() in text.casefold()


def audit_hard_filters(filters: CandidateFilters, attrs: CandidateAttrs) -> list[Violation]:
    """复核一条候选人是否真的满足已下推的硬条件。

    「条件里没有」或「候选人属性未知」都不算违反：抽检只报可证实的违反。
    字段口径严格对齐 ``LanceDBSearchIndex._where``：数值比大小、城市/意向地/方向按
    列表元素求交、姓名/公司/职位/学校按子串、学校等级按包含式层级。
    """
    out: list[Violation] = []

    def bad(field: str, value: object, detail: str) -> None:
        out.append(Violation("hard_filter", field, str(value), detail))

    if filters.min_years is not None and attrs.total_years is not None:
        if attrs.total_years + 1e-9 < filters.min_years:
            bad("min_years", filters.min_years, f"候选人 {attrs.total_years}")
    if filters.max_years is not None and attrs.total_years is not None:
        if attrs.total_years - 1e-9 > filters.max_years:
            bad("max_years", filters.max_years, f"候选人 {attrs.total_years}")
    if filters.min_age is not None and attrs.age is not None:
        if attrs.age < filters.min_age:
            bad("min_age", filters.min_age, f"候选人 {attrs.age}")
    if filters.max_age is not None and attrs.age is not None:
        if attrs.age > filters.max_age:
            bad("max_age", filters.max_age, f"候选人 {attrs.age}")
    if filters.highest_degree and attrs.highest_degree:
        candidate_degree = normalize_degree(attrs.highest_degree)
        # 与 _where 一致：非精确限定走 IN (包含式层级)，精确限定走等值。
        if candidate_degree not in filters.degree_values():
            bad("highest_degree", filters.highest_degree,
                f"候选人 {candidate_degree}{'（精确）' if filters.degree_exact else ''}")
    if filters.max_qs_rank is not None and attrs.qs_rank is not None:
        if attrs.qs_rank > filters.max_qs_rank:
            bad("max_qs_rank", filters.max_qs_rank, f"候选人 {attrs.qs_rank}")
    school_values = filters.school_level_values()
    if school_values and attrs.school_tags and not _intersects(attrs.school_tags, school_values):
        bad("school_level", filters.school_level, f"候选人 {'、'.join(attrs.school_tags)}")
    if filters.school_region and attrs.school_region:
        if filters.school_region != attrs.school_region:
            bad("school_region", filters.school_region, f"候选人 {attrs.school_region}")
    wanted = filters.location_values()
    if wanted and attrs.locations and not _intersects(attrs.locations, wanted):
        bad("locations", "、".join(wanted), f"候选人 {'、'.join(attrs.locations)}")
    wanted_preferred = filters.preferred_location_values()
    if wanted_preferred and attrs.preferred_locations and not _intersects(attrs.preferred_locations, wanted_preferred):
        bad("preferred_locations", "、".join(wanted_preferred), f"候选人 {'、'.join(attrs.preferred_locations)}")
    if filters.name and attrs.name_text and not _contains(attrs.name_text, filters.name):
        bad("name", filters.name, "姓名未命中")
    if filters.company and attrs.company_text and not _contains(attrs.company_text, filters.company):
        bad("company", filters.company, "无公司命中")
    if filters.companies and attrs.company_text and not any(
            _contains(attrs.company_text, company) for company in filters.companies):
        bad("companies", "、".join(filters.companies), "无公司命中")
    if filters.title and attrs.title_text and not _contains(attrs.title_text, filters.title):
        bad("title", filters.title, "无职位命中")
    if filters.school and attrs.school_text and not _contains(attrs.school_text, filters.school):
        bad("school", filters.school, "无学校命中")
    if filters.exclude_skills and attrs.skills_text:
        for skill in filters.exclude_skills:
            if has_skill(attrs.skills_text, skill):
                bad("exclude_skills", skill, "候选人命中被排除的技能")
    wanted_directions = (*filters.career_directions, *filters.career_specializations,
                         *filters.business_directions)
    if wanted_directions and attrs.direction_values and not _intersects(attrs.direction_values, wanted_directions):
        bad("career_directions", "、".join(wanted_directions), f"候选人 {'、'.join(attrs.direction_values)}")
    return out


def audit_candidate(filters: CandidateFilters,
                    rows: Sequence[CandidateAttrs]) -> list[Violation]:
    """候选人级复核：**任一行满足即算满足**。

    同一候选人可能有多个当前修订，每个修订各有一条父 chunk（``_snapshot`` 会为每个
    修订生成父行），索引侧只要有一行过 ``_where`` 该候选人就会被返回；因此不能拿
    「第一行」的属性去判违反，否则会报出假违规。
    """
    if not rows:
        return []
    violations: list[Violation] | None = None
    for row in rows:
        found = audit_hard_filters(filters, row)
        if not found:
            return []
        if violations is None or len(found) < len(violations):
            violations = found
    return violations or []


def summarize_violations(violations: Iterable[Violation]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in violations:
        counts[item.kind] = counts.get(item.kind, 0) + 1
    return dict(sorted(counts.items()))


def percentile(values: Sequence[float], ratio: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    return ordered[max(0, min(len(ordered) - 1, int(round(len(ordered) * ratio)) - 1))]


def summarize_plans(records: Sequence[Mapping[str, object]]) -> dict:
    """解析产物的分布汇总：回退率、来源分布、采纳字段、解析延迟分位。

    ``records`` 每项至少要带 ``degraded``（None 表示未回退）、``source``、``elapsed_ms``。
    """
    total = len(records)
    degraded = [item.get("degraded") for item in records if item.get("degraded")]
    sources: dict[str, int] = {}
    accepted: dict[str, int] = {}
    for item in records:
        source = str(item.get("source") or "rule")
        sources[source] = sources.get(source, 0) + 1
        for field in item.get("accepted_fields") or ():
            accepted[str(field)] = accepted.get(str(field), 0) + 1
    latencies = [float(item["elapsed_ms"]) for item in records if item.get("elapsed_ms") is not None]
    return {
        "queries": total,
        "degraded_total": len(degraded),
        "degraded_rate": round(len(degraded) / total, 4) if total else None,
        "degraded_kinds": {str(k): degraded.count(k) for k in dict.fromkeys(degraded)},
        "source_distribution": dict(sorted(sources.items())),
        "accepted_field_distribution": dict(sorted(accepted.items())),
        "parse_ms_p50": round(percentile(latencies, 0.5), 1) if latencies else None,
        "parse_ms_p95": round(percentile(latencies, 0.95), 1) if latencies else None,
        "parse_ms_max": round(max(latencies), 1) if latencies else None,
    }


def summarize_conditions(records: Sequence[Mapping[str, object]]) -> dict:
    """逐条统计条件覆盖率：多少查询产出了哪类条件，用于判断「字段是否真的启用」。"""
    present: dict[str, int] = {}
    inferred: dict[str, int] = {}
    for item in records:
        for field, confidence in (item.get("conditions") or {}).items():
            present[str(field)] = present.get(str(field), 0) + 1
            if confidence == "inferred":
                inferred[str(field)] = inferred.get(str(field), 0) + 1
    return {
        "field_present": dict(sorted(present.items())),
        "field_inferred": dict(sorted(inferred.items())),
    }


def satisfies_filters(filters: CandidateFilters, attrs: CandidateAttrs) -> bool:
    """是否有**正面证据**同时满足全部条件（离线算选择性用）。

    与 ``audit_hard_filters`` 的分工：后者对「属性未知」不判违反（保守，避免把缺失当
    违规），这里反过来——未知/空视为**不满足**，因为索引侧 ``_where`` 对 NULL/空列不会
    匹配（``NULL >= X`` 为 NULL、``array_has_any([], ..)`` 为假、``LIKE`` 对空串为假），
    所以「未知」在可召性上等价于「不满足」。这样算出的选择性仍然只是**上界**（还忽略
    召回窗口），但比把未知算成满足紧得多。
    """
    if filters.min_years is not None and attrs.total_years is None:
        return False
    if filters.max_years is not None and attrs.total_years is None:
        return False
    if filters.min_age is not None and attrs.age is None:
        return False
    if filters.max_age is not None and attrs.age is None:
        return False
    if filters.highest_degree and attrs.highest_degree is None:
        return False
    if filters.max_qs_rank is not None and attrs.qs_rank is None:
        return False
    if filters.school_level and not attrs.school_tags:
        return False
    if filters.school_region and not attrs.school_region:
        return False
    if filters.location_values() and not attrs.locations:
        return False
    if filters.preferred_location_values() and not attrs.preferred_locations:
        return False
    for value, text in ((filters.name, attrs.name_text), (filters.company, attrs.company_text),
                        (filters.title, attrs.title_text), (filters.school, attrs.school_text)):
        if value and not text:
            return False
    if filters.companies and not attrs.company_text:
        return False
    wanted_directions = (*filters.career_directions, *filters.career_specializations,
                         *filters.business_directions)
    if wanted_directions and not attrs.direction_values:
        return False
    return not audit_hard_filters(filters, attrs)


def condition_selectivity(filters: CandidateFilters,
                          attrs: Mapping[str, Sequence["CandidateAttrs"]]) -> dict:
    """离线算每条条件能过多少候选人（**不发检索请求**）。

    用途：解释「开启解析后结果被清空」到底是哪一条条件造成的。返回的是**上界**——
    未知属性按「不满足」计，但仍忽略召回窗口，所以真实可召数只可能更少；
    因此「上界为 0」就是「该条件组合结构性无解」的确证。
    """
    per_field: dict[str, int] = {}
    conditions: list[tuple[str, CandidateFilters]] = [
        ("min_years", CandidateFilters(min_years=filters.min_years)),
        ("max_years", CandidateFilters(max_years=filters.max_years)),
        ("min_age", CandidateFilters(min_age=filters.min_age)),
        ("max_age", CandidateFilters(max_age=filters.max_age)),
        ("highest_degree", CandidateFilters(highest_degree=filters.highest_degree,
                                            degree_exact=filters.degree_exact)),
        ("max_qs_rank", CandidateFilters(max_qs_rank=filters.max_qs_rank)),
        ("school_level", CandidateFilters(school_level=filters.school_level)),
        ("school_region", CandidateFilters(school_region=filters.school_region)),
        ("locations", CandidateFilters(locations=filters.location_values())),
        ("preferred_locations", CandidateFilters(preferred_locations=filters.preferred_location_values())),
        ("name", CandidateFilters(name=filters.name)),
        ("company", CandidateFilters(company=filters.company)),
        ("companies", CandidateFilters(companies=filters.companies)),
        ("title", CandidateFilters(title=filters.title)),
        ("school", CandidateFilters(school=filters.school)),
        ("exclude_skills", CandidateFilters(exclude_skills=filters.exclude_skills)),
        ("career_directions", CandidateFilters(career_directions=filters.career_directions)),
        ("career_specializations", CandidateFilters(
            career_specializations=filters.career_specializations)),
        ("business_directions", CandidateFilters(
            business_directions=filters.business_directions)),
    ]

    def passing(rows: Sequence[CandidateAttrs], single: CandidateFilters) -> bool:
        return any(satisfies_filters(single, row) for row in rows)

    for field, single in conditions:
        if not _has_filter(single):
            continue
        per_field[field] = sum(1 for rows in attrs.values() if passing(rows, single))
    return {
        "candidates": len(attrs),
        "satisfying_all": sum(1 for rows in attrs.values() if passing(rows, filters)),
        "per_field": dict(sorted(per_field.items())),
    }


def _has_filter(filters: CandidateFilters) -> bool:
    return any(value not in (None, "", (), []) for value in (
        filters.min_years, filters.max_years, filters.min_age, filters.max_age,
        filters.highest_degree, filters.max_qs_rank, filters.school_level,
        filters.school_region, filters.location_values(), filters.preferred_location_values(),
        filters.name, filters.company, filters.companies, filters.title, filters.school,
        filters.exclude_skills, filters.career_directions, filters.career_specializations,
        filters.business_directions,
    ))
