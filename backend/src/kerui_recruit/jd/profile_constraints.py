"""JD 候选人画像中的显式硬条件解析与评估。

把画像文本里的明确硬条件（卡 985、字节或阿里背景、LangGraph 优先）解析成可检查、
可修改的结构化约束。约束只来自人工画像文本或明确语义，不把「优先」升级为 MUST。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# 学校档次别名：画像里常见的表达归一为结构化可核验值。
_SCHOOL_LEVEL_ALIASES: dict[str, tuple[str, ...]] = {
    "985": ("985", "卡985", "985高校"),
    "211": ("211", "卡211", "211高校"),
    "双一流": ("双一流",),
    "硕士": ("硕士", "研究生"),
    "博士": ("博士",),
}

# 公司经历别名（用于 company_history 词法核验的同义公司名）。
_COMPANY_ALIASES: dict[str, tuple[str, ...]] = {
    "字节": ("字节跳动", "字节"),
    "阿里": ("阿里巴巴", "阿里"),
    "腾讯": ("腾讯",),
    "蚂蚁": ("蚂蚁集团", "蚂蚁"),
    "百度": ("百度",),
}

# 明确硬条件触发词（出现在画像中即视为 MUST），避免把普通描述误判为硬条件。
_MUST_MARKERS = ("卡", "必须", "硬性要求", "必要条件", "缺一不可", "必备")
# 优先/加分触发词 → PLUS。
_PLUS_MARKERS = ("优先", "加分", "更佳", "最好", "prefer", "plus")
# 「或」为组内 OR 连接词。
_OR_TOKENS = ("或", "或者", "、", "/", ",", "，", ";")


@dataclass(frozen=True, slots=True)
class ExactConstraint:
    kind: str          # school_level | company_history | skill | industry | other_keyword
    operator: str      # OR | AND
    alternatives: tuple[str, ...]
    strength: str      # MUST | PLUS | EXCLUDE
    source: str        # manual | inferred
    source_text: str


# 合法取值域：与匹配侧 evaluate_exact_constraints 的分派一一对应。
_KINDS = frozenset({"school_level", "company_history", "skill", "industry", "other_keyword"})
_OPERATORS = frozenset({"OR", "AND"})
_STRENGTHS = frozenset({"MUST", "PLUS", "EXCLUDE"})

# 硬条件字段口径：JD 解析提示词与「按文本重解析硬条件」共用同一份，避免两处漂移。
CONSTRAINT_FIELD_SPEC = (
    "exact_constraints：显式硬条件数组，每项含 "
    "kind（school_level | company_history | skill | industry | other_keyword）、"
    "operator（OR | AND，同一项内多个 alternatives 之间的关系，默认 OR）、"
    "alternatives（字符串数组，命中任一即算满足）、"
    "strength（MUST | PLUS | EXCLUDE）、"
    "source（固定 inferred）、"
    "source_text（依据的原文片段，逐字复制，不要改写）。"
    "判定口径（严格遵守，MUST 误判会直接淘汰候选人）："
    "① 只有原文明确写「必须 / 硬性要求 / 必要条件 / 缺一不可 / 必备 / 卡」才算 MUST，其余一律 PLUS；"
    "② 按单句判定：「985 优先，必须熟悉 Java」里 985 是 PLUS、Java 才是 MUST；"
    "③ 岗位职责、泛能力描述、行业罗列不产出约束；"
    "④ EXCLUDE 只用于原文明确排除项（如「不考虑外包背景」）；"
    "⑤ 没有明确硬条件时输出空数组，不要凑数。"
)

CONSTRAINT_PARSE_PROMPT = """你是招聘系统的硬条件抽取器，负责从 JD 候选人画像要求里抽取显式硬条件。

{spec}

补充规则：
- 只依据给定文本判断，不引入外部常识、不补全未写明的条件；
- 画像文本通常是一段寻访口径，只有其中写明「必须/卡/必备」或明确排除的才是硬条件；
- 只输出 JSON 对象，不要 markdown 代码块或任何多余文字。

画像要求文本：
{source_text}"""


def normalize_constraints(raw) -> list[dict]:
    """把模型或人工给出的硬条件规整为可落库的契约形态。

    宁缺毋滥：kind/strength/operator 非法的整条丢弃（匹配侧拿到含义不明的约束会静默失效）；
    **MUST 必须带 source_text 原文依据，没有依据的降级为 PLUS** —— MUST 未命中会直接硬拒候选人，
    依据不足时不能让它具备淘汰力。
    """
    if not isinstance(raw, (list, tuple)):
        return []
    normalized: list[dict] = []
    seen: set[tuple[str, str, tuple[str, ...]]] = set()
    for item in raw:
        if hasattr(item, "model_dump"):
            item = item.model_dump()
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "").strip()
        strength = str(item.get("strength") or "").strip().upper()
        operator = str(item.get("operator") or "OR").strip().upper() or "OR"
        if kind not in _KINDS or strength not in _STRENGTHS or operator not in _OPERATORS:
            continue
        alternatives = tuple(
            dict.fromkeys(
                str(value).strip() for value in (item.get("alternatives") or []) if str(value).strip()
            )
        )
        if not alternatives:
            continue
        source_text = str(item.get("source_text") or "").strip()
        if strength == "MUST" and not source_text:
            strength = "PLUS"
        key = (kind, strength, tuple(a.casefold() for a in alternatives))
        if key in seen:
            continue
        seen.add(key)
        normalized.append({
            "kind": kind,
            "operator": operator,
            "alternatives": list(alternatives),
            "strength": strength,
            "source": str(item.get("source") or "inferred"),
            "source_text": source_text,
        })
    return normalized


def _contains_any(text: str, markers: tuple[str, ...]) -> bool:
    folded = text.casefold()
    return any(m.casefold() in folded for m in markers)


def _split_alternatives(text: str) -> list[str]:
    """按 OR 连接词切分为备选列表，去空去重保序。"""
    parts = re.split(r"[或,，、;/]", text)
    return [p.strip() for p in parts if p.strip()]


def _split_statements(text: str) -> list[str]:
    """按句读/分号/逗号切分为局部语句，使 MUST/PLUS 判定只作用于同一语句。"""
    return [s.strip() for s in re.split(r"[。！？；;，,\n]", text) if s.strip()]


def _strength_for(clause: str, default: str) -> str:
    """在单个局部语句内判定 MUST/PLUS；无触发词时用 default。"""
    if _contains_any(clause, _MUST_MARKERS):
        return "MUST"
    if _contains_any(clause, _PLUS_MARKERS):
        return "PLUS"
    return default


def parse_exact_constraints(text: str | None, *, source: str = "manual") -> list[ExactConstraint]:
    """从一段画像文本解析出显式硬条件（纯函数，不依赖模型）。

    按局部语句判定 MUST/PLUS：例如「985优先，必须熟悉Java」中 985 是 PLUS（优先），
    不会因后句的「必须」被升级为 MUST；「字节或阿里背景优先」是 PLUS。没有明确
    触发词时不产出约束，避免把普通描述误升级为硬条件。
    """
    if not text:
        return []
    result: list[ExactConstraint] = []

    for statement in _split_statements(text):
        # 学校档次：卡 985 / 985 等。
        for canonical, aliases in _SCHOOL_LEVEL_ALIASES.items():
            for alias in aliases:
                if re.search(rf"(?:卡|必须|要求|优先|加分|更佳|最好)?\s*{re.escape(alias)}", statement):
                    strength = _strength_for(statement, default="PLUS")
                    result.append(ExactConstraint(
                        kind="school_level", operator="OR", alternatives=(canonical,),
                        strength=strength, source=source, source_text=alias,
                    ))
                    break

        # 公司历史：提取「字节或阿里背景」「字节、阿里经历」等 OR 组（同一语句内的公司为组内 OR）。
        if re.search(r"背景|工作经历|经历", statement):
            companies: list[str] = []
            for canonical, aliases in _COMPANY_ALIASES.items():
                if any(alias in statement for alias in aliases):
                    companies.append(canonical)
            if companies:
                strength = _strength_for(statement, default="MUST")
                result.append(ExactConstraint(
                    kind="company_history", operator="OR", alternatives=tuple(companies),
                    strength=strength, source=source, source_text=statement,
                ))

        # 技能优先：LangGraph 优先 → PLUS；技能必须 → MUST。
        for match in re.finditer(r"([A-Za-z][\w\s+#./-]{1,30}?)\s*(优先|加分|更佳|必须|必备)", statement):
            skill = match.group(1).strip()
            strength = "MUST" if match.group(2) in ("必须", "必备") else "PLUS"
            result.append(ExactConstraint(
                kind="skill", operator="OR", alternatives=(skill,),
                strength=strength, source=source, source_text=match.group(0),
            ))

    # 去重（同 kind + alternatives 视为同一约束）。
    seen: set[tuple] = set()
    deduped: list[ExactConstraint] = []
    for constraint in result:
        key = (constraint.kind, constraint.operator, tuple(a.casefold() for a in constraint.alternatives))
        if key not in seen:
            seen.add(key)
            deduped.append(constraint)
    return deduped


def evaluate_exact_constraints(
    constraints: list[ExactConstraint],
    candidate_parsed: dict,
) -> tuple[list[str], list[str]]:
    """返回 (未满足的 MUST 描述, 满足的 PLUS 描述)。

    只有 MUST 未满足才会硬拒；PLUS 仅用于加分排序，不硬拒。
    """
    unmet_must: list[str] = []
    met_plus: list[str] = []
    for constraint in constraints:
        hit = _constraint_hit(constraint, candidate_parsed)
        if constraint.strength == "MUST" and not hit:
            unmet_must.append(constraint.source_text or " or ".join(constraint.alternatives))
        elif constraint.strength == "PLUS" and hit:
            met_plus.append(constraint.source_text or " or ".join(constraint.alternatives))
    return unmet_must, met_plus


def _constraint_hit(constraint: ExactConstraint, candidate_parsed: dict) -> bool:
    alternatives = [a.casefold() for a in constraint.alternatives]
    if constraint.kind == "school_level":
        return _school_level_hit(alternatives, candidate_parsed)
    if constraint.kind == "company_history":
        return _company_hit(alternatives, candidate_parsed)
    # skill / industry / other_keyword：词法命中。
    text = _candidate_lexical_text(candidate_parsed)
    return any(a in text for a in alternatives)


def _school_level_hit(levels: list[str], candidate_parsed: dict) -> bool:
    school_level = str(candidate_parsed.get("school_level") or "").casefold()
    school_tier = str(candidate_parsed.get("school_tier") or "").casefold()
    tags: set[str] = set()
    for edu in candidate_parsed.get("educations") or []:
        if isinstance(edu, dict):
            tags.update(str(t).casefold() for t in (edu.get("school_tags") or []))
    degree = str(candidate_parsed.get("highest_degree") or "").casefold()
    for level in levels:
        if level in (school_level, school_tier) or level in tags:
            return True
        if level in ("硕士", "博士") and level in degree:
            return True
    return False


def _company_hit(companies: list[str], candidate_parsed: dict) -> bool:
    parts = [str(candidate_parsed.get("current_company") or "")]
    for exp in candidate_parsed.get("experiences") or []:
        if isinstance(exp, dict):
            parts.append(str(exp.get("company") or ""))
    company_text = " ".join(p for p in parts if p).casefold()
    for company in companies:
        if company in company_text:
            return True
        for alias in _COMPANY_ALIASES.get(company, ()):
            if alias.casefold() in company_text:
                return True
    return False


def _candidate_lexical_text(candidate_parsed: dict) -> str:
    parts = [
        str(candidate_parsed.get("summary") or ""),
        " ".join(str(s) for s in (candidate_parsed.get("skills") or [])),
    ]
    for exp in candidate_parsed.get("experiences") or []:
        if isinstance(exp, dict):
            parts.append(str(exp.get("summary") or ""))
            parts.append(str(exp.get("title") or ""))
    for proj in candidate_parsed.get("projects") or []:
        if isinstance(proj, dict):
            parts.append(str(proj.get("summary") or ""))
            parts.append(str(proj.get("tech_stack") or ""))
    return " ".join(parts).casefold()
