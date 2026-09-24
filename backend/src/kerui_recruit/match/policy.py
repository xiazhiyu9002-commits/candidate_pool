"""统一 JD-候选人配对评估器：资格 + 技能覆盖 + 核心职责证据。

JD 找人（match_jd）与人找 JD（reverse match）复用同一 `evaluate_pair`，
保证同一对对象结论一致。资格（职业方向/业务方向/必需技能）为硬条件，不被软特征补偿。
"""
from __future__ import annotations

from dataclasses import dataclass

from kerui_recruit.direction.policy import confirmed_multi_directions
from kerui_recruit.match.service import _skill_coverage, _skill_text
from kerui_recruit.search.lexicon import tokenize_lexical_text
from kerui_recruit.search.query import normalize_skill


@dataclass(frozen=True, slots=True)
class PairDecision:
    eligibility: str  # "eligible" | "pending" | "rejected"
    hard_reasons: tuple[str, ...] = ()
    matched_skills: tuple[str, ...] = ()
    missing_skills: tuple[str, ...] = ()
    duty_evidence: tuple[str, ...] = ()
    # 业务方向是否一致：True 命中、False 不命中、None 任一侧缺业务方向。
    # 只用于排序分层（业务一致者置顶并保留配比名额），**不再淘汰**。
    business_match: bool | None = None
    # PLUS（优先/加分）项命中率：命中数 / 总数；该 JD 没有 PLUS 项时为 None。
    # 降级后的 skill / other_keyword 靠它进入排序（见 service._score_context 的 preference 分量）。
    preference: float | None = None


# 职责文本中的动作/泛词，不参与证据判断（只保留领域名词）。
_DUTY_STOPWORDS = {
    "负责", "主导", "参与", "设计", "开发", "构建", "搭建", "优化", "提升",
    "完成", "落地", "实现", "推动", "建设", "维护", "支持", "相关", "业务",
    "系统", "平台", "体系", "流程", "方案", "能力", "经验", "熟悉", "掌握",
}


_MAX_MUST_SKILLS = 5  # 解析易过度指定必备技能；只取前 5 个，避免「必须全命中」导致无人匹配。


def _must_skills_from_parsed(jd_parsed: dict) -> list[str]:
    # 只取 required_skills（单一技能词）；requirements 里的 MUST 技能多为长短语/重复，不再纳入硬条件。
    skills = [normalize_skill(s) for s in (jd_parsed.get("required_skills") or [])]
    seen: set[str] = set()
    result: list[str] = []
    for skill in skills:
        key = skill.casefold()
        if skill and key not in seen:
            seen.add(key)
            result.append(skill)
    return result[:_MAX_MUST_SKILLS]


# 泛能力/术语 → 具体技能与项目证据的同义映射。用于避免把「关系型数据库」「前端工程化」
# 这类泛词当作字面硬筛，导致具备 MySQL/组件库/构建工程等具体证据的人被误拒。
_SKILL_SYNONYMS: dict[str, tuple[str, ...]] = {
    "关系型数据库": ("mysql", "postgresql", "oracle", "sql server", "sql", "mariadb"),
    "前端工程化": ("webpack", "vite", "rollup", "组件库", "微前端", "构建工具", "工程化", "babel", "esbuild"),
}


def _alternative_hit(candidate_parsed: dict, alternative: str) -> bool:
    """单个必备技能备选是否命中（含同义映射）。"""
    matched, _ = _skill_coverage(candidate_parsed, [alternative])
    if matched:
        return True
    key = normalize_skill(alternative).casefold()
    for synonym in _SKILL_SYNONYMS.get(key, ()):
        matched, _ = _skill_coverage(candidate_parsed, [synonym])
        if matched:
            return True
    return False


def _must_skill_groups_from_parsed(jd_parsed: dict) -> list[list[str]]:
    """返回必备技能组（组内 OR、组间 AND）。

    有 ``must_skill_groups`` 时使用它；否则回退到旧 ``required_skills`` 前 5 个，
    每个技能作为独立 AND 组（保持旧语义）。
    """
    groups = jd_parsed.get("must_skill_groups") or []
    result: list[list[str]] = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        alternatives = [normalize_skill(a) for a in (group.get("alternatives") or []) if a]
        alternatives = list(dict.fromkeys(a for a in alternatives if a))
        if alternatives:
            result.append(alternatives)
    if result:
        return result
    return [[s] for s in _must_skills_from_parsed(jd_parsed)]


def _duty_has_evidence(duty: str, candidate_text: str) -> bool:
    duty_tokens = [t for t in tokenize_lexical_text(duty) if t not in _DUTY_STOPWORDS]
    if not duty_tokens:
        return False
    candidate_tokens = set(tokenize_lexical_text(candidate_text))
    return sum(1 for t in duty_tokens if t in candidate_tokens) / len(duty_tokens) >= 0.5


def _direction_hit(jd_parsed: dict, candidate_parsed: dict) -> bool | None:
    """职业方向是否有交集：有交集 True、无交集 False、证据不足 None（不淘汰）。

    优先按**细分**比较（合同要求「匹配只按细分」）；当任一侧细分缺失或不可比（旧词表）时
    回退到**大类**比较，既保住存量未回填简历的方向硬门槛，又避免新旧词表被误判成不一致。
    任一侧完全没有可用方向信息（空/OTHER/非法）时返回 None。
    """
    jd_career, jd_specs, _ = confirmed_multi_directions(jd_parsed)
    cand_career, cand_specs, _ = confirmed_multi_directions(candidate_parsed)
    if jd_specs and cand_specs:
        return bool(set(jd_specs) & set(cand_specs))
    if jd_career and cand_career:
        return bool(set(jd_career) & set(cand_career))
    return None


def _business_hit(jd_parsed: dict, candidate_parsed: dict) -> bool | None:
    """业务方向是否一致：有交集 True、无交集 False、任一侧缺失 None。

    仅作为**排序分层标记**返回，不再参与硬淘汰——业务方向只在召回后做「一致者置顶 + 保留
    配比名额」的排序处理，避免把业务不完全对口但能力强的候选人直接淘汰。
    """
    _, _, jd_business = confirmed_multi_directions(jd_parsed)
    _, _, cand_business = confirmed_multi_directions(candidate_parsed)
    if not jd_business or not cand_business:
        return None
    return bool(set(jd_business) & set(cand_business))


def _exact_constraint_state(
    jd_parsed: dict,
    candidate_parsed: dict,
) -> tuple[tuple[str, ...], float | None]:
    """显式硬条件的两种回传：淘汰理由（MUST 未满足 + EXCLUDE 命中）、PLUS 命中率（排序用）。

    两者的分派都在 ``profile_constraints`` 里，取值域只有一处，避免这里另抄一份判定口径。
    淘汰理由自带前缀（``exact_constraint:`` / ``exclude_constraint:``），便于区分「没达到门槛」
    与「命中了排除项」。
    """
    raw = jd_parsed.get("exact_constraints") or []
    if not raw:
        return (), None
    from kerui_recruit.jd.profile_constraints import (
        ExactConstraint,
        evaluate_exact_constraints,
        exclusion_hits,
        preference_ratio,
    )
    constraints = [ExactConstraint(**c) if isinstance(c, dict) else c for c in raw]
    unmet_must, _ = evaluate_exact_constraints(constraints, candidate_parsed)
    reasons = [f"exact_constraint:{u}" for u in unmet_must]
    reasons += [f"exclude_constraint:{e}" for e in exclusion_hits(constraints, candidate_parsed)]
    return tuple(reasons), preference_ratio(constraints, candidate_parsed)


def evaluate_pair(jd_parsed: dict, candidate_parsed: dict) -> PairDecision:
    """统一配对评估（纯函数，JD 找人 / 人找 JD 共用）。

    - 职业方向：两侧都有方向时必须有交集（按细分；细分缺失则回退大类），否则 rejected。
    - 业务方向：**不再淘汰**，只回传 ``business_match`` 供排序分层（一致者置顶 + 保留配比名额）。
    - 任一侧方向缺失（未解析、待核）时不硬过滤，避免伤及存量数据。
    - 必需技能（AND/OR 语义）0 命中 → rejected，不靠软特征补偿。
    - 显式硬条件的 MUST 未满足、或命中 EXCLUDE 项 → rejected；
      PLUS 只回传 ``preference`` 命中率供排序加权。
    - 否则 eligible，并附带技能覆盖与核心职责证据。
    """
    business_match = _business_hit(jd_parsed, candidate_parsed)
    if _direction_hit(jd_parsed, candidate_parsed) is False:
        return PairDecision("rejected", ("career_direction_mismatch",),
                            business_match=business_match)

    must_groups = _must_skill_groups_from_parsed(jd_parsed)
    matched_all: list[str] = []
    missing_all: list[str] = []
    if jd_parsed.get("must_skill_groups"):
        # 新 JD：组内 OR、组间 AND（明确硬条件）。
        for group in must_groups:
            hit = next((a for a in group if _alternative_hit(candidate_parsed, a)), None)
            if hit is not None:
                matched_all.append(hit)
            else:
                missing_all.append(" or ".join(group))
        if missing_all:
            return PairDecision(
                "rejected", ("must_skills_missing",),
                matched_skills=tuple(matched_all), missing_skills=tuple(missing_all),
                business_match=business_match,
            )
    else:
        # 旧 JD 只有 required_skills：作为软特征，至少命中一项才不排除；
        # 零命中才 rejected（避免旧解析把泛词/可替代框架拆成独立 MUST 造成全库漏检）。
        required = _must_skills_from_parsed(jd_parsed)
        matched_all = [s for s in required if _alternative_hit(candidate_parsed, s)]
        # 缺口必须如实记录：只在「全部未命中」时才记，会让 missing_skills 长期为空，
        # 上层（分层 / AI 复核 / 前端）看不到「JD 明确要求但候选人确实没有」的那些项。
        missing_all = [s for s in required if s not in matched_all]
        if required and not matched_all:
            return PairDecision(
                "rejected", ("must_skills_missing",),
                matched_skills=tuple(matched_all), missing_skills=tuple(required),
                business_match=business_match,
            )

    # 显式硬条件（卡 985、字节或阿里等）：MUST 未满足、或命中 EXCLUDE 项，都不能靠向量高分补偿
    # → rejected；PLUS（优先/加分）按命中率参与排序加权，不硬拒。
    rejections, preference = _exact_constraint_state(jd_parsed, candidate_parsed)
    if rejections:
        return PairDecision("rejected", rejections, business_match=business_match)

    duties = jd_parsed.get("core_duties") or []
    candidate_text = _skill_text(candidate_parsed)
    duty_evidence = tuple(d for d in duties if _duty_has_evidence(d, candidate_text))

    return PairDecision(
        "eligible",
        matched_skills=tuple(matched_all), missing_skills=tuple(missing_all),
        duty_evidence=duty_evidence,
        business_match=business_match,
        preference=preference,
    )
