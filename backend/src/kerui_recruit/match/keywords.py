"""匹配查询关键词：只保留「技术」与「业务」两类，按 token 预算裁剪。

设计依据见 `.trae/documents/检索与匹配重构方案-2026-09-20.md` §2。

来源（只用结构化字段 + 可靠的 requirement label）：

- 技术词：JD = ``required_skills`` + ``requirements``（技能类 label）；
  候选人 = ``skills`` + 各段经历的 ``tech_stack``。
- 业务词：JD = ``business_directions`` 中文名 + ``plus_industry`` + ``plus_project_types``
  + ``requirements``（业务类 label）；候选人 = ``business_directions`` 中文名。

明确排除：证书、软技能、管理经验、领导力、学历、年限、地点、职责、成果、专业、背景，
以及 label 语义模糊的「能力 / 项目 / 项目类型 / 知识 / AI实践」——同一 label 下混装技术
与软性描述，规则无法区分，宁缺毋滥。

自由文本（JD 的 ``core_duties``、候选人的画像与项目业务场景）不进 FTS query，
只保留在向量通道与证据包里：实测候选人项目业务场景平均 98 token、最多 679 token，
放进查询会把关键词通道重新稀释成主题噪声。
"""
from __future__ import annotations

from collections.abc import Iterable

from kerui_recruit.direction.policy import (
    BUSINESS_DIRECTION_LABELS,
    confirmed_multi_directions,
)
from kerui_recruit.search.lexicon import tokenize_lexical_text

# requirements 里可安全区分为技术 / 业务的 label 白名单（见方案 §2.1 A 桶）。
TECH_LABELS = frozenset({"技能", "skill", "技术", "技术栈", "工具"})
BIZ_LABELS = frozenset({"行业", "业务", "领域知识", "业务领域", "场景"})

# token 预算按 FTS 实际分词口径计（jieba + 技术标记保护），实测余量见方案 §2.2。
TECH_TOKEN_BUDGET = 48
BIZ_TOKEN_BUDGET = 24


def _as_list(value: object) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if item]
    return [str(value)]


def _requirement_values(parsed: dict, labels: frozenset[str]) -> list[str]:
    values: list[str] = []
    for item in parsed.get("requirements") or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("kind") or "").upper() not in ("MUST", "PLUS"):
            continue
        if str(item.get("label") or "").strip() not in labels:
            continue
        values.append(str(item.get("value") or ""))
    return values


def _dedup(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = " ".join(str(value).split())
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(text)
    return result


# 技能/技术要求里混装的「句子式」值（如「精通Hadoop生态（Hive/Spark/Flink/Kafka等）」）：
# 它是职责式表述而不是技能词。这类值**不许吞并**别的词——否则 required_skills 里
# Hadoop / Hive / Spark 会因为它含了这些 token 被整片删掉，查询反而退化成一句长噪声
# （实测「数仓技术TL」的 5 个短词被 1 句长句吞掉，tech 词只剩 2 条）。
# 它们只作为预算有余量时的补充：`required_skills` 排在前面，长句落在尾部被 _fit_budget 截掉。
_DESCRIPTIVE_MARKERS = (
    "精通", "熟练", "深入理解", "了解", "掌握", "熟悉", "使用", "具备", "理解",
    "能把", "能做", "至少一种", "至少1个", "经验", "能力", "原理", "链路", "流程", "设计",
)


def _can_swallow(value: str) -> bool:
    """该值是否有资格吞并被它完全覆盖的短词（句子式表述没有资格）。"""
    return not any(marker in value for marker in _DESCRIPTIVE_MARKERS)


def _drop_contained(values: list[str]) -> list[str]:
    """丢掉词级被别人完全覆盖的短词（``Python`` ⊂ ``Python or Java``）。

    但覆盖者必须是**精炼词**：``Python or Java`` 这类 OR 写法吞并短词是想要的；
    ``精通Hadoop生态（Hive/Spark/Flink/Kafka等）`` 这类句子吞并则是反向的——它把技能词
    删掉、把长句留下，正是方案 §2 要排除的「职责长句稀释查询」。
    """
    token_sets = [(value, set(tokenize_lexical_text(value))) for value in values]
    kept: list[str] = []
    for index, (value, tokens) in enumerate(token_sets):
        if not tokens:
            continue
        covered = any(
            tokens < other and _can_swallow(other_value)
            for other_index, (other_value, other) in enumerate(token_sets)
            if other_index != index
        )
        if not covered:
            kept.append(value)
    return kept


def _fit_budget(values: list[str], budget: int) -> list[str]:
    """按 token 预算从尾部截断；首个词即使超预算也保留（否则会产出空查询）。"""
    kept: list[str] = []
    used = 0
    for value in values:
        cost = len(tokenize_lexical_text(value))
        if kept and used + cost > budget:
            break
        kept.append(value)
        used += cost
    return kept


def _business_labels(parsed: dict) -> list[str]:
    _, _, business = confirmed_multi_directions(parsed)
    return [BUSINESS_DIRECTION_LABELS.get(code, code) for code in business]


def build_jd_tech_terms(parsed: dict) -> list[str]:
    values = _as_list(parsed.get("required_skills")) + _requirement_values(parsed, TECH_LABELS)
    return _fit_budget(_drop_contained(_dedup(values)), TECH_TOKEN_BUDGET)


def build_jd_biz_terms(parsed: dict) -> list[str]:
    values = _business_labels(parsed)
    values += _as_list(parsed.get("plus_industry"))
    values += _as_list(parsed.get("plus_project_types"))
    values += _requirement_values(parsed, BIZ_LABELS)
    return _fit_budget(_drop_contained(_dedup(values)), BIZ_TOKEN_BUDGET)


def build_candidate_tech_terms(parsed: dict) -> list[str]:
    values = _as_list(parsed.get("skills"))
    for experience in parsed.get("experiences") or []:
        if isinstance(experience, dict):
            values += _as_list(experience.get("tech_stack"))
    return _fit_budget(_drop_contained(_dedup(values)), TECH_TOKEN_BUDGET)


def build_candidate_biz_terms(parsed: dict) -> list[str]:
    return _fit_budget(_drop_contained(_dedup(_business_labels(parsed))), BIZ_TOKEN_BUDGET)


def build_jd_query_text(parsed: dict) -> str:
    """岗位匹配人的 FTS query：技术词 + 业务词（不含职责长句）。"""
    return " ".join((*build_jd_tech_terms(parsed), *build_jd_biz_terms(parsed)))


def build_candidate_query_text(parsed: dict) -> str:
    """人匹配岗位的 FTS query：技术词 + 业务方向标签（不含姓名/学校/城市/年限等筛选字段）。"""
    return " ".join((*build_candidate_tech_terms(parsed), *build_candidate_biz_terms(parsed)))
