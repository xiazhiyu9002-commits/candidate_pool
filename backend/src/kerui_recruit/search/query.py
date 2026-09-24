from __future__ import annotations

import re
from dataclasses import dataclass, replace

from kerui_recruit.search.cities import (
    CITY_GROUPS,
    CITY_NAMES,
    DISTRICT_SUFFIXES,
    PROVINCE_PREFIXES,
)
from kerui_recruit.search.contracts import CandidateFilters
from kerui_recruit.search.degrees import DEGREE_ALIASES, normalize_degree
from kerui_recruit.search.lexicon import LexicalConcept, concepts_from_query, normalize_skill


@dataclass(frozen=True, slots=True)
class ParsedCondition:
    """一个从查询文本解析出的硬条件及其来源置信度。"""

    field: str
    value: str
    confidence: str  # "explicit"（显式语法） | "inferred"（裸词推断）


@dataclass(frozen=True, slots=True)
class ParsedQuery:
    keywords: str
    filters: CandidateFilters
    concepts: tuple[LexicalConcept, ...] = ()
    conditions: tuple[ParsedCondition, ...] = ()


# 查询侧城市词典：规则解析与 LLM 解析的枚举白名单共用同一份（全国地级市 + 直辖市 + 港澳）。
# 索引侧 ``location_terms`` 的归一化用同一份表（``search/cities.py``），两侧口径必须逐字一致。
LOCATIONS = CITY_NAMES

_SCHOOL_LEVELS = ("985", "211", "双一流", "海外", "普通")

# 城市名后紧跟这些学校后缀时，属于学校实体（如「北京大学」「上海交通大学」），不作为地点。
_SCHOOL_SUFFIXES = (
    "大学", "学院", "学校", "师范", "交通", "财经", "理工", "医科",
    "农业", "政法", "外国语", "邮电", "海洋", "石油", "电力", "工业",
    "科技", "航天", "航空", "建筑", "林业", "地质", "矿业", "民族",
)

# 地点剥离：一次扫描吃掉「省级前缀 + 城市（或北上广深组合写法） + 市/省后缀」，
# 避免留下「广东省」「市」这类无意义关键词。城市名后紧跟学校后缀（北京大学）或
# 区县后缀（朝阳区）时不剥离，保留原词交给词法召回。
_LOCATION_STRIP_RE = re.compile(
    rf"(?:(?:{'|'.join(re.escape(name) for name in sorted(PROVINCE_PREFIXES, key=len, reverse=True))}))?"
    rf"(?:{'|'.join(re.escape(name) for name in sorted((*CITY_GROUPS, *CITY_NAMES), key=len, reverse=True))})"
    rf"(?!{'|'.join(_SCHOOL_SUFFIXES)}|{'|'.join(DISTRICT_SUFFIXES)})"
    rf"(?:市|省)?"
)

# 毕业年份：4 位数字 + 年 + 毕业/届/级/入学，不能当作工作经验年限。
_GRADUATION_RE = re.compile(r"\d{4}\s*年\s*(?:毕业|届|级|入学)")
# 4 位年份（含"毕业于2020年"这类前置表达）：一律不作为年限。
_FOUR_DIGIT_YEAR_RE = re.compile(r"(?<!\d)(?:19|20)\d{2}\s*年")

# 年限范围：3-5年 / 3到5年 / 3至5年
_YEAR_RANGE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:-|~|～|到|至)\s*(\d+(?:\.\d+)?)\s*年")
# 上限：以内/以下/至多/不超过
_YEAR_MAX_RE = re.compile(
    r"(?:至多|最多|不超过|上限)\s*(\d+(?:\.\d+)?)\s*年"
    r"|(\d+(?:\.\d+)?)\s*年\s*(?:以内|以下|之内)"
)
# 下限：以上/及以上/至少/不少于
_YEAR_MIN_RE = re.compile(
    r"(?:至少|不少于|不低于)\s*(\d+(?:\.\d+)?)\s*年"
    r"|(\d+(?:\.\d+)?)\s*年\s*(?:以上|及以上)"
)
# 裸年限：限制 1-2 位数字，天然排除 4 位年份。
_BARE_YEAR_RE = re.compile(r"(\d{1,2}(?:\.\d+)?)\s*年")

_QS_RE = re.compile(r"QS\s*(?:前|排名|前)?\s*(\d+)", re.IGNORECASE)

# 软否定：不要求/不需要/不必须 -> 不产生正向条件，也不排除（中性）。
_SOFT_NEGATE_RE = re.compile(r"(?:不要求|不需要|不必须|无需)\s*([A-Za-z][A-Za-z0-9+#]*)")
_SOFT_NEGATE_DEGREE_RE = re.compile(r"(?:不要求|不需要|不必须|无需)\s*(本科|硕士|博士|大专|学士|专科)")
# 硬排除：排除/不要/不用/拒绝 -> 排除概念（英文技能或连续中文词，如「外包」）。
# 「不要(?!求)」避免把「不要求」里的「不要」误判为硬排除。
_EXCLUDE_RE = re.compile(r"(?:排除|不用|拒绝|不要(?!求))\s*([A-Za-z][A-Za-z0-9+#]*|[\u4e00-\u9fff]+)")


def has_skill(content: str, skill: str) -> bool:
    """词边界匹配，避免 Java 误匹配 JavaScript、C++ 匹配 C。"""
    if not skill:
        return False
    if skill.isascii() and any(ch.isalpha() for ch in skill):
        return (
            re.search(
                rf"(?<![A-Za-z0-9+#]){re.escape(skill)}(?![A-Za-z0-9+#])",
                content,
                re.IGNORECASE,
            )
            is not None
        )
    return skill.casefold() in content.casefold()


def parse_query(
    text: str,
    *,
    school_alias_groups: dict[str, tuple[str, ...]] | None = None,
) -> ParsedQuery:
    """从自然语言中确定性提取硬条件，其余保留为关键词。

    规则优先：年限、学历、地点、学校等级、QS、排除条件均可离线识别，
    不依赖 LLM；失败时仍保留可解释的基础查询能力。
    """
    filters = CandidateFilters()

    min_years, max_years = _parse_years(text)
    filters = replace(filters, min_years=min_years, max_years=max_years)

    degree, degree_exact = _parse_degree(text)
    filters = replace(filters, highest_degree=degree, degree_exact=degree_exact)

    locations, preferred = _parse_locations(text)
    filters = replace(
        filters,
        location=locations[0] if locations else None,
        locations=locations,
        preferred_location=preferred[0] if preferred else None,
        preferred_locations=preferred,
    )

    qs_match = _QS_RE.search(text)
    if qs_match:
        filters = replace(filters, max_qs_rank=int(qs_match.group(1)))

    school_level = _match_school_level(text)
    if school_level:
        filters = replace(filters, school_level=school_level)

    exclude = _parse_excludes(text)
    filters = replace(filters, exclude_skills=exclude)

    keywords = _strip_conditions(text)
    concepts = concepts_from_query(keywords, extra_alias_groups=school_alias_groups)
    conditions = _build_conditions(filters, text)
    return ParsedQuery(keywords=keywords, filters=filters, concepts=concepts, conditions=conditions)


def _build_conditions(filters: CandidateFilters, text: str) -> tuple[ParsedCondition, ...]:
    """从最终 filters 反向构建可解释的硬条件列表（含来源置信度）。"""
    conditions: list[ParsedCondition] = []
    has_marker = any(m in text for m in ("现居", "目前", "常驻", "居住", "期望", "意向", "求职", "目标", "想去"))
    location_conf = "explicit" if has_marker else "inferred"
    for loc in filters.locations or ():
        conditions.append(ParsedCondition("location", loc, location_conf))
    for loc in filters.preferred_locations or ():
        conditions.append(ParsedCondition("preferred_location", loc, "explicit"))
    if filters.min_years is not None:
        conditions.append(ParsedCondition("min_years", f"{filters.min_years:g}年", "explicit"))
    if filters.max_years is not None:
        conditions.append(ParsedCondition("max_years", f"{filters.max_years:g}年", "explicit"))
    if filters.highest_degree:
        conditions.append(ParsedCondition("highest_degree", filters.highest_degree, "explicit"))
    if filters.max_qs_rank is not None:
        conditions.append(ParsedCondition("max_qs_rank", f"QS前{filters.max_qs_rank}", "explicit"))
    if filters.school_level:
        conditions.append(ParsedCondition("school_level", filters.school_level, "explicit"))
    for skill in filters.exclude_skills or ():
        conditions.append(ParsedCondition("exclude_skills", skill, "explicit"))
    return tuple(conditions)


def _parse_years(text: str) -> tuple[float | None, float | None]:
    # 先剔除毕业/年份片段，避免「2020年毕业」「毕业于2020年」被当作工作年限。
    cleaned = _FOUR_DIGIT_YEAR_RE.sub(" ", text)
    cleaned = _GRADUATION_RE.sub(" ", cleaned)

    match = _YEAR_RANGE_RE.search(cleaned)
    if match:
        return float(match.group(1)), float(match.group(2))

    match = _YEAR_MAX_RE.search(cleaned)
    if match:
        return None, float(match.group(1) or match.group(2))

    match = _YEAR_MIN_RE.search(cleaned)
    if match:
        return float(match.group(1) or match.group(2)), None

    match = _BARE_YEAR_RE.search(cleaned)
    if match:
        value = float(match.group(1))
        if value >= 1900:  # 兜底：裸 4 位数字按年份处理，不作为年限。
            return None, None
        return value, None

    return None, None


def _parse_degree(text: str) -> tuple[str | None, bool]:
    # 软否定：不要求/不需要 + 学历 → 不产生硬过滤，学历词也不作为正向条件。
    if re.search(r"(?:不要求|不需要|不必须|无需)\s*(本科|硕士|博士|大专|学士|专科)", text):
        return None, False

    # 精确限定：仅/只/只要/限定 + 学历（「必须」是强调，不触发精确限定）。
    exact = re.search(r"(?:仅|只|只要|限定)\s*(本科|硕士|博士|大专|学士|专科)", text)
    if exact:
        return normalize_degree(exact.group(1)), True

    # 优先：软条件，不产生硬过滤。
    if re.search(r"(本科|硕士|博士|大专|学士|专科)\s*优先", text):
        return None, False

    # 不限：明确不限制。
    if "学历不限" in text or "不限学历" in text:
        return None, False

    # 默认「本科」= 本科及以上。
    degree = _match_degree(text)
    if degree:
        return degree, False
    return None, False


def _is_school_entity(text: str, match) -> bool:
    """城市名 token 后紧跟学校后缀（如「北京」+「大学」）时判定为学校实体。"""
    return text[match.end():].startswith(_SCHOOL_SUFFIXES)


def _is_district_reference(text: str, match) -> bool:
    """城市名后紧跟区/县/镇/乡时属于区县写法（「朝阳区」）。

    与索引侧 ``cities.canonical_city`` 同一口径：朝阳既是辽宁地级市、也是北京/长春的区，
    语义不确定时不当作地点条件，词本身留在关键词里照常参与词法召回。
    """
    return text[match.end():].startswith(DISTRICT_SUFFIXES)


def _parse_locations(text: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """分离现居地与求职意向地（意向地可为多值或关系）。

    城市名后紧跟学校后缀（如「北京大学」「上海交通大学」）时属于学校实体，
    不作为地点；显式「现居/期望」等标记后的城市名仍按地点提取。
    「北上广深」这类组合写法展开成多个城市。
    """
    preferred_markers = ("期望", "意向", "求职", "目标", "想去")
    current_markers = ("现居", "目前", "常驻", "居住")
    tokens = "|".join((*preferred_markers, *current_markers, *CITY_GROUPS, *LOCATIONS))
    locations: list[str] = []
    preferred: list[str] = []
    destination = locations
    for match in re.finditer(tokens, text):
        token = match.group()
        if token in preferred_markers:
            destination = preferred
        elif token in current_markers:
            destination = locations
        elif token in CITY_GROUPS:
            for city in CITY_GROUPS[token]:
                if city not in destination:
                    destination.append(city)
        elif token in LOCATIONS and _is_school_entity(text, match):
            continue  # 学校实体，不作为地点
        elif token in LOCATIONS and _is_district_reference(text, match):
            continue  # 区县写法（「朝阳区」），语义不确定，不作为地点
        elif token not in destination:
            destination.append(token)
    return tuple(locations), tuple(preferred)


def _parse_excludes(text: str) -> tuple[str, ...]:
    excludes: list[str] = []
    for match in _EXCLUDE_RE.finditer(text):
        token = normalize_skill(match.group(1))
        if token and token not in excludes:
            excludes.append(token)
    return tuple(excludes)


def _match_degree(text: str) -> str | None:
    folded = text.casefold()
    for token, normalized in sorted(
        DEGREE_ALIASES.items(), key=lambda item: len(item[0]), reverse=True
    ):
        if token.casefold() in folded:
            return normalized
    return None


def _match_school_level(text: str) -> str | None:
    for token in _SCHOOL_LEVELS:
        if token in text:
            return token
    return None


def _strip_conditions(text: str) -> str:
    cleaned = _FOUR_DIGIT_YEAR_RE.sub(" ", text)
    cleaned = _GRADUATION_RE.sub(" ", cleaned)
    cleaned = _YEAR_RANGE_RE.sub(" ", cleaned)
    cleaned = _YEAR_MAX_RE.sub(" ", cleaned)
    cleaned = _YEAR_MIN_RE.sub(" ", cleaned)
    cleaned = _BARE_YEAR_RE.sub(" ", cleaned)
    cleaned = _QS_RE.sub(" ", cleaned)
    # 先处理否定/排除（在剥离学历词之前，避免「不要求」与后续技能粘连）。
    cleaned = _SOFT_NEGATE_DEGREE_RE.sub(" ", cleaned)
    cleaned = _SOFT_NEGATE_RE.sub(" ", cleaned)
    cleaned = _EXCLUDE_RE.sub(" ", cleaned)
    for token in DEGREE_ALIASES:
        cleaned = re.sub(re.escape(token), " ", cleaned, flags=re.IGNORECASE)
    cleaned = _LOCATION_STRIP_RE.sub(" ", cleaned)
    for token in _SCHOOL_LEVELS:
        cleaned = cleaned.replace(token, " ")
    # 去掉剩余修饰词。
    cleaned = re.sub(r"(仅|只|只要|限定|必须|优先|不限|学历|期望|意向|求职|目标|想去|现居|目前|常驻|居住)", " ", cleaned)
    cleaned = re.sub(r"[，,；;]", " ", cleaned)
    cleaned = re.sub(r"(?<!\S)(或|和|及)(?!\S)", " ", cleaned)
    return " ".join(cleaned.split())
