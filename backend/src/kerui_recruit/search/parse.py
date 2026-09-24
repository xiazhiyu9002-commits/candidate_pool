"""查询文本的 LLM 结构化解析：一次调用产出 filters + keywords + semantic_query。

设计（见 docs/superpowers/specs/2026-09-21-query-parse-hybrid-body-design.md）：
- **默认关闭**（API 侧的 ``parse_enabled``）；开启后逐字段校验，某字段不过只回退该字段的规则值，
  不整体丢弃；LLM 不可用/超时/校验拒绝时完整回退 ``parse_query``（原词直查），degraded 语义不变；
- **只结构化原文已出现的信息**：实体类字段（公司/学校/职位/姓名/手机号）必须能在原文定位，
  禁止推断补全技能、行业、资历；
- **方向三字段走 A+C 交叉验证**：LLM 选码（A）与分类器词表反查（C）互相印证，命中即硬筛选，
  映射不到就不产出该条件（词仍留在 keywords / semantic_query）；
- 语义查询复用 ``search/rewrite.py`` 的既有校验器，不通过就静默使用原查询。
"""
from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, replace

from pydantic import BaseModel, Field

from kerui_recruit.direction.classifier import direction_codes_from_text
from kerui_recruit.direction.policy import (
    BUSINESS_DIRECTION_LABELS,
    CAREER_DIRECTION_LABELS,
    SPECIALIZATION_LABELS,
    direction_field_for_code,
    normalize_direction_value,
    render_business_taxonomy,
    render_career_taxonomy,
)
from kerui_recruit.providers.ai.contracts import ExecutionContext
from kerui_recruit.search.contracts import CandidateFilters
from kerui_recruit.search.degrees import normalize_degree
from kerui_recruit.search.lexicon import LEXICON_VERSION, concepts_from_query, tokenize_lexical_text
from kerui_recruit.search.query import LOCATIONS, ParsedCondition, parse_query
from kerui_recruit.search.rewrite import semantic_query_is_valid

# 提示词版本：独立于 LEXICON_VERSION，升级后不会命中旧缓存。
PARSE_PROMPT_VERSION = "1"

# 解析预算。**2026-09-22 从 2.5 秒提到 20 秒**：2.5 秒是按「极快的非思考模型」定的，
# 实测同一段解析提示词（2797 字）——阿里 `qwen3.8-flash` 关思考 3.9~8.8 秒、
# 智谱 `glm-5.3-flashx`（强制思考，无法关闭）9.8~17.8 秒，全都超过 2.5 秒。
# 结果是**每一次解析都超时**、静默回退规则链路（`source=rule / degraded=timeout`），
# 任务组 8 的「先判能否硬筛、不能则软排」在生产里根本走不到。
#
# 取值理由：20 秒覆盖实测最慢的一档（17.8 秒）并留一点余量；它不再与 FTS / embedding /
# 重排抢同一份预算——`api/search.py` 只在 `parse_enabled` 时把这 20 秒加在检索预算之上。
PARSE_TIMEOUT_SECONDS = 20.0
_PARSE_CACHE_SIZE = 256
_PARSE_TTL_SECONDS = 600.0
_UNPARSED_MAX_TERMS = 20

_SCHOOL_LEVELS = ("985", "211", "双一流", "海外", "普通")
_SCHOOL_REGIONS = ("domestic", "overseas")
_GENDERS = ("男", "女")
_DEGREE_LABELS = ("博士", "硕士", "本科", "大专", "专科")
_YEAR_LIMITS = (0.0, 80.0)
_AGE_LIMITS = (16, 80)
# 属地枚举在原文里的触发词：区域 code 本身不会出现在查询里，必须靠这些词印证。
_REGION_CUES = {"overseas": ("海外", "国外", "留学", "境外"), "domestic": ("国内", "境内")}
_DIRECTION_FIELD_NAMES = ("career_directions", "career_specializations", "business_directions")
_LABELS_BY_CODE = {
    **CAREER_DIRECTION_LABELS, **SPECIALIZATION_LABELS, **BUSINESS_DIRECTION_LABELS,
}

# 必须能在原文定位的字段：禁止 LLM 造学校、姓名。
_SPAN_FIELDS = ("school", "name", "phone")
_SPAN_LIST_FIELDS = ("locations", "preferred_locations", "exclude_skills")
# 「先判断能不能硬筛，不能就退化为软排」的字段（任务组 8）。
# title / company：索引侧都是「整串子串匹配」（`title_text`/`company_text` LIKE '%值%'），
# 而查询与 JD 里的写法往往是描述性长短语（「经验丰富的软件工程团队负责人（VP）」
# 「自动化测试/QA工程师」）或不完整的公司简称，整串几乎不可能逐字出现在简历字段里：
# 2026-09-21 抽检里 11 条空结果有 10 条由这两个条件造成（单条可选择到 0~1 人）。
#
# 所以这里**既下推为硬条件、又并入词条**，两者缺一不可：
# - 下推硬条件：公司名写全时（「字节跳动」）理应真的筛，精度比软排高；
# - 并入词条：硬筛被判定为「筛空」而退化时，条件从 where 里消失，词条仍在
#   FTS / 向量 / 重排里参与打分——否则「退化为软排」会变成「悄悄丢掉条件」。
#
# 到底保不保留硬筛，由 `search/service.py:_relax_unmatchable_filters` 逐条探测决定；
# 面板手填的值不走这条路径，恒为硬筛（手填意图明确，筛空就该是 0 结果）。
_SOFT_FIELDS = ("title", "company", "companies")


class ParsedSearchPlan(BaseModel):
    """LLM 解析输出：全部字段可空，未提及即为 None / 空列表。"""

    min_years: float | None = None
    max_years: float | None = None
    min_age: int | None = None
    max_age: int | None = None
    highest_degree: str | None = None
    degree_exact: bool = False
    locations: list[str] = Field(default_factory=list)
    preferred_locations: list[str] = Field(default_factory=list)
    max_qs_rank: int | None = None
    school_level: str | None = None
    school_region: str | None = None
    school: str | None = None
    company: str | None = None
    companies: list[str] = Field(default_factory=list)
    title: str | None = None
    name: str | None = None
    phone: str | None = None
    gender: str | None = None
    exclude_skills: list[str] = Field(default_factory=list)
    career_directions: list[str] = Field(default_factory=list)
    career_specializations: list[str] = Field(default_factory=list)
    business_directions: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    semantic_query: str = ""


@dataclass(frozen=True, slots=True)
class ParsedPlan:
    """解析结果（含回退链状态），供 API 回显与检索取用。"""

    filters: CandidateFilters
    keywords: str
    concepts: tuple
    conditions: tuple[ParsedCondition, ...]
    semantic_query: str | None = None
    unparsed_terms: tuple[str, ...] = ()
    source: str = "rule"  # rule | llm | mixed
    degraded: str | None = None  # provider_error | timeout | rejected
    # LLM 实际被采纳的字段名（逐字段回退后仍生效的那些），供 API 标注条件来源。
    accepted_fields: tuple[str, ...] = ()


_PARSE_PROMPT = """你是招聘人才搜索的查询解析器。把使用者写的一句自然语言需求拆成三份产物：
硬条件（filters）、词法词条（keywords）、语义查询（semantic_query）。

只允许把**原文里已经出现**的信息结构化，禁止推断补全技能、框架、职责、行业、资历、学历或地点。
未提及的字段一律留空（null 或空数组）。原文里没有的证据不要写进任何字段。

字段要求：
- 年限/年龄：只能来自原文的明确表述（「3-5 年」「35 岁以下」）；给区间时同时给上下限。
- 学历：原文写了层次才填（博士/硕士/本科/大专/专科）；「只要硕士」这类精确限定置 degree_exact=true。
- locations / preferred_locations：只能填城市名，且必须是原文里出现过的城市；「现居/目前」→locations，
  「期望/意向/想去」→preferred_locations；原文没写归属时按现居处理。
- company / companies：公司名（原文出现才填；「或」关系放 companies）。只填能逐字出现在简历里的
  公司全名或通用简称；描述性长短语（「专注金融科技的公司」）不要填，留在 keywords 里。
- title：职位名（原文出现才填）。只填标准职位名（「Java 工程师」「产品经理」）；带修饰语的
  长短语（「经验丰富的软件工程团队负责人（VP）」）不要填，留在 keywords 里。
  这两类字段会被试作硬条件，**筛不出人时自动退化为词条参与排序**：填准的收益是精度，
  填不准会被错误收窄结果，拿不准就不填。
- school：学校名（原文出现才填，写标准名）。
- name / phone / gender：只有原文明确写出才填；手机号必须是原文中的完整 11 位号码。
- exclude_skills：原文明确排除的技能/背景（「排除外包」「不要 PHP」）。
- 方向三字段（career_directions 职业大类 / career_specializations 职业细分 / business_directions 业务方向）：
  只能从下面的词表里选 code，且必须有原文依据；拿不准就不填，把那个词留在 keywords 里。
- keywords：交给关键词检索的词条，保留原文实义词的规范化写法（可用公认缩写/别名，如 JS→JavaScript）。
- semantic_query：给向量与重排用的一条保真查询，遵守：保留原文全部概念、只规范无歧义别名、
  不新增任何条件、不得输出 OR/括号/分类清单、不超过原文长度的 2 倍且最多 80 字。

职业方向词表（code（中文））：
{career_taxonomy}

业务方向词表（code（中文））：
{business_taxonomy}

只返回 JSON（不要 markdown、不要解释），结构如下：
{{"min_years":null,"max_years":null,"min_age":null,"max_age":null,"highest_degree":null,
"degree_exact":false,"locations":[],"preferred_locations":[],"max_qs_rank":null,
"school_level":null,"school_region":null,"school":null,"company":null,"companies":[],
"title":null,"name":null,"phone":null,"gender":null,"exclude_skills":[],
"career_directions":[],"career_specializations":[],"business_directions":[],
"keywords":[],"semantic_query":""}}

查询文本：
{query}"""


def _flat(text: str) -> str:
    return re.sub(r"\s+", "", str(text or "")).casefold()


def _has_span(value: str, text: str) -> bool:
    """值能否在原文里定位（忽略空白与大小写）。"""
    needle = _flat(value)
    return bool(needle) and needle in _flat(text)


def _number_span(value: float | int, text: str) -> bool:
    """数字类条件必须能在原文找到对应的数字，避免模型凭空加年限/年龄。"""
    number = int(value) if float(value).is_integer() else value
    return str(number) in text


def _degree_from_text(text: str) -> set[str]:
    return {
        code for label in _DEGREE_LABELS
        if label in text and (code := normalize_degree(label)) is not None
    }


def _validate_degree(value: str | None, text: str) -> str | None:
    if not value:
        return None
    code = normalize_degree(value)
    return code if code and code in _degree_from_text(text) else None


def _validate_enum(value: str | None, allowed: tuple[str, ...], text: str, *, require_span: bool) -> str | None:
    if not value or value not in allowed:
        return None
    return value if (not require_span or value in text or _has_span(value, text)) else None


def _validate_region(value: str | None, text: str) -> str | None:
    """属地 code（domestic/overseas）必须由原文的「海外/国内」类词印证。"""
    if not value or value not in _SCHOOL_REGIONS:
        return None
    return value if any(cue in text for cue in _REGION_CUES[value]) else None


def _validate_ranges(plan: ParsedSearchPlan) -> bool:
    """区间合法性：min <= max、且都落在允许区间内。"""
    for low, high, (floor, ceiling) in (
        (plan.min_years, plan.max_years, _YEAR_LIMITS),
        (plan.min_age, plan.max_age, _AGE_LIMITS),
    ):
        for value in (low, high):
            if value is None:
                continue
            if not floor <= value <= ceiling:
                return False
        if low is not None and high is not None and low > high:
            return False
    return True


def _validated_directions(plan: ParsedSearchPlan, text: str) -> dict[str, list[str]]:
    """方向字段 A+C 交叉验证：词表印证（C）或与枚举标签精确一致时才采用（A）。"""
    confirmed = direction_codes_from_text(text)
    result: dict[str, list[str]] = {}
    for raw in (*plan.career_directions, *plan.career_specializations, *plan.business_directions):
        code = normalize_direction_value(raw)
        if not code:
            continue
        field = direction_field_for_code(code)
        if field is None:
            continue
        # C 有 → 采用；C 无 → 只有「**原文里**出现该枚举的标准中文标签或 code」才采用。
        # 注意不能拿 LLM 自己的输出当依据：提示词要求它回 code，`raw` 天然等于 code，
        # 那样写等于没有校验（实测会把「AI 效能 全栈」解析出 OPS/ALGORITHM 等一堆方向）。
        if code not in confirmed:
            label = _LABELS_BY_CODE.get(code, "")
            if not _has_span(label, text) and not _has_span(code, text):
                continue
        result.setdefault(field, []).append(code)
    return result


def _apply_llm_fields(plan: ParsedSearchPlan, rule: CandidateFilters, text: str) -> tuple[CandidateFilters, dict[str, object]]:
    """逐字段校验后覆盖规则值；返回（合并后的 filters, 被采纳的字段值）。"""
    accepted: dict[str, object] = {}

    def take(name: str, value):
        if value not in (None, "", [], ()):
            accepted[name] = value

    if _validate_ranges(plan):
        take("min_years", plan.min_years)
        take("max_years", plan.max_years)
        take("min_age", plan.min_age)
        take("max_age", plan.max_age)
    valid_degree = _validate_degree(plan.highest_degree, text)
    if valid_degree:
        accepted["highest_degree"] = valid_degree
        accepted["degree_exact"] = bool(plan.degree_exact)
    take("max_qs_rank", plan.max_qs_rank if (plan.max_qs_rank and _number_span(plan.max_qs_rank, text)) else None)
    take("school_level", _validate_enum(plan.school_level, _SCHOOL_LEVELS, text, require_span=True))
    take("school_region", _validate_region(plan.school_region, text))
    take("gender", _validate_enum(plan.gender, _GENDERS, text, require_span=True))
    for name in _SPAN_FIELDS:
        value = getattr(plan, name)
        if value and _has_span(value, text):
            take(name, str(value).strip())
    for name in _SPAN_LIST_FIELDS:
        values = [str(item).strip() for item in (getattr(plan, name) or []) if _has_span(str(item), text)]
        take(name, tuple(dict.fromkeys(values)))
    # 职位/公司：下推为**可退化的**硬条件（详见 `_SOFT_FIELDS` 注释），
    # 同时由 `_soft_field_values` 并入词条，保证退化后仍有软排信号。
    for name in ("title", "company"):
        value = getattr(plan, name)
        if value and _has_span(value, text):
            take(name, str(value).strip())
    take("companies", tuple(dict.fromkeys(
        str(item).strip() for item in (plan.companies or []) if _has_span(str(item), text)
    )))
    for name in ("locations", "preferred_locations"):
        values = [item for item in (accepted.get(name) or ()) if item in LOCATIONS]
        accepted.pop(name, None)
        take(name, tuple(values))
    for field, codes in _validated_directions(plan, text).items():
        take(field, tuple(dict.fromkeys(codes)))

    changes: dict[str, object] = {}
    for name, value in accepted.items():
        if name == "locations":
            changes["locations"] = value
            changes["location"] = value[0] if value else None
        elif name == "preferred_locations":
            changes["preferred_locations"] = value
            changes["preferred_location"] = value[0] if value else None
        else:
            changes[name] = value
    return replace(rule, **changes), accepted


def _soft_field_values(plan: ParsedSearchPlan, text: str) -> tuple[str, ...]:
    """软字段值（只做排序信号）：仅在原文能定位时保留，避免把幻觉词条塞进检索。

    软字段既有单值（title）也有多值（companies），统一按条目处理。
    """
    values: list[str] = []
    for name in _SOFT_FIELDS:
        raw = getattr(plan, name, None)
        items = raw if isinstance(raw, (list, tuple)) else ([raw] if raw else [])
        for item in items:
            value = str(item).strip()
            if value and _has_span(value, text):
                values.append(value)
    return tuple(dict.fromkeys(values))


def _unparsed_terms(text: str, keywords: str, semantic: str | None, accepted: dict[str, object]) -> tuple[str, ...]:
    """原文实义词里既没进条件、也没进词条/语义查询的残句（供排障与回显）。"""
    covered = _flat(keywords) + _flat(semantic or "")
    for value in accepted.values():
        if isinstance(value, (tuple, list)):
            covered += "".join(_flat(item) for item in value)
        elif not isinstance(value, bool):
            covered += _flat(value)
    leftovers: list[str] = []
    for token in tokenize_lexical_text(text):
        if len(token) < 2 or token in covered:
            continue
        leftovers.append(token)
    return tuple(dict.fromkeys(leftovers))[:_UNPARSED_MAX_TERMS]


class QueryParser:
    """LLM 查询解析器：带缓存、逐字段校验与规则回退。"""

    def __init__(self, client, *, cache_size: int = _PARSE_CACHE_SIZE,
                 ttl_seconds: float = _PARSE_TTL_SECONDS,
                 timeout_seconds: float = PARSE_TIMEOUT_SECONDS) -> None:
        self._client = client
        self._cache_size = cache_size
        self._ttl_seconds = ttl_seconds
        self._timeout_seconds = timeout_seconds
        self._cache: dict[str, ParsedPlan] = {}
        self._cache_time: dict[str, float] = {}

    def _cache_key(self, text: str) -> str:
        identity = getattr(self._client, "cache_identity", None) or getattr(self._client, "model", "unspecified")
        return f"{identity}|{PARSE_PROMPT_VERSION}|{LEXICON_VERSION}|{_flat(text)}"

    def _cached(self, key: str) -> ParsedPlan | None:
        timestamp = self._cache_time.get(key)
        if timestamp is None:
            return None
        if time.monotonic() - timestamp >= self._ttl_seconds:
            self._cache.pop(key, None)
            self._cache_time.pop(key, None)
            return None
        return self._cache[key]

    def _store(self, key: str, plan: ParsedPlan) -> ParsedPlan:
        self._cache[key] = plan
        self._cache_time[key] = time.monotonic()
        while len(self._cache) > self._cache_size:
            oldest = next(iter(self._cache))
            self._cache.pop(oldest, None)
            self._cache_time.pop(oldest, None)
        return plan

    async def parse(self, text: str, *, school_alias_groups=None,
                    deadline_monotonic: float | None = None) -> ParsedPlan:
        rule = parse_query(text, school_alias_groups=school_alias_groups)
        base = ParsedPlan(
            filters=rule.filters, keywords=rule.keywords, concepts=rule.concepts,
            conditions=rule.conditions, semantic_query=None, unparsed_terms=(), source="rule",
        )
        if self._client is None or not text.strip():
            return base
        key = self._cache_key(text)
        cached = self._cached(key)
        if cached is not None:
            return cached

        timeout = self._timeout_seconds
        if deadline_monotonic is not None:
            timeout = max(0.1, min(timeout, deadline_monotonic - time.monotonic()))
        prompt = _PARSE_PROMPT.format(
            career_taxonomy=render_career_taxonomy(),
            business_taxonomy=render_business_taxonomy(),
            query=text,
        )
        try:
            plan = await asyncio.wait_for(
                self._client.complete_json(
                    [{"role": "user", "content": prompt}], ParsedSearchPlan,
                    execution_context=ExecutionContext.INTERACTIVE,
                ),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            return replace(base, degraded="timeout")
        except Exception:  # noqa: BLE001 - 解析失败必须完整回退规则链路
            return replace(base, degraded="provider_error")

        filters, accepted = _apply_llm_fields(plan, rule.filters, text)

        keywords = " ".join(str(item).strip() for item in plan.keywords if str(item).strip())
        if not keywords:
            keywords = rule.keywords
        # 软字段（职位名）不下推硬筛，改为排序信号：原文定位得到的职位短语并入词条，
        # 否则它只会留在 unparsed_terms 里，对排序毫无影响。
        soft_terms = [value for value in _soft_field_values(plan, text)
                      if _flat(value) not in _flat(keywords)]
        if soft_terms:
            keywords = " ".join([keywords, *soft_terms]).strip()
        semantic = plan.semantic_query.strip() or None
        if semantic and not semantic_query_is_valid(text, semantic):
            semantic = None
        if not accepted and semantic is None and keywords == rule.keywords:
            # 模型既没给出可用条件，也没给出词条或语义查询 → 视为无效解析，整条回退规则链路。
            return replace(base, degraded="rejected")
        concepts = concepts_from_query(keywords, extra_alias_groups=school_alias_groups)
        conditions = tuple(
            ParsedCondition(field=name, value=_condition_text(value), confidence="inferred")
            for name, value in accepted.items()
        ) + tuple(rule.conditions)
        # 规则链路识别到的条件若没被 LLM 覆盖，最终生效集合里就同时存在两套来源 → mixed。
        rule_fields = {_canonical_field(condition.field) for condition in rule.conditions}
        source = "llm" if not (rule_fields - {_canonical_field(name) for name in accepted}) else "mixed"
        return self._store(key, ParsedPlan(
            filters=filters, keywords=keywords, concepts=concepts, conditions=conditions,
            semantic_query=semantic,
            unparsed_terms=_unparsed_terms(text, keywords, semantic, accepted),
            source=source,
            accepted_fields=tuple(accepted),
        ))


def _condition_text(value: object) -> str:
    if isinstance(value, (tuple, list)):
        return "、".join(str(item) for item in value)
    if isinstance(value, bool):
        return "精确" if value else ""
    return str(value)


# 规则解析用的是单值字段名，多值字段才是下推用的字段名：比较来源时先归一。
_SINGLETON_ALIASES = {"location": "locations", "preferred_location": "preferred_locations"}


def _canonical_field(name: str) -> str:
    return _SINGLETON_ALIASES.get(name, name)
