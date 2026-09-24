"""画像双形态结构化契约：一次生成共享 facts + narrative + points + compact。

一致性要求：每个关键事实必须同时体现在 narrative 与 points 两种展示形态中；
任一关键事实缺失即判为不一致（待核），调用方不得把旧整体/新分点混在一起保留。
"""
from __future__ import annotations

import logging
import re
import time
from typing import Awaitable, Callable

from pydantic import BaseModel, Field, field_validator

from kerui_recruit.providers import profile_spec

# 句末标点：分点自身已以此收尾时，拼接整体段落不再补「。」。
_CLAUSE_TERMINATORS = ("。", "！", "？", "；", "…", "，", ".", "!", "?", ";", ",")

# 分点只按**句末标点**切分。「，」「、」是句内停顿，不切——按逗号切会把
# 「3年后端经验，现任京东后端开发工程师。」拆成两行，界面上就成了「一词一行」。
_SENTENCE_BREAK = re.compile(r"[。！？；;\n]+")

# 判为「碎片」的长度阈值：只用于**识别**需要修复的存量分点，不用于合并文本。
_FRAGMENT_CHARS = 12

# 画像重写总预算：初稿 + 一次重写共用的墙钟上限，超预算就返回当前最好版本。
REPAIR_BUDGET_SECONDS = 90.0
# 「重新生成画像」接口的服务端硬上限：必须**大于** REPAIR_BUDGET_SECONDS，否则预算还没用尽
# 接口就先超时了，重写闸门形同不存在（这条关系由 `test_regen_timeout_exceeds_repair_budget` 守着）。
# 超时点上给明确文案而不是让前端一直转圈——这是问题 #6 的一半。
REGEN_TIMEOUT_SECONDS = 150.0


def split_by_sentence(text: str) -> list[str]:
    """按句末标点切分，保留每段文本本身（不做任何增删）。"""
    return [part.strip() for part in _SENTENCE_BREAK.split(text or "") if part.strip()]


def repair_profile_points(narrative: str, points: list[str]) -> list[str] | None:
    """存量分点的**修复**：只在能证明它确实是被按逗号切碎时，才用整体段落重算分点。

    为什么要判据而不是一律重算：模型产出的分点是「最核心的 3~5 条」——一个**子集**，
    直接按整体段落重算会把它扩成全部句子，那是在改语义而不是修显示。

    判为「被切碎」需要同时成立：
    1. 存在短于 ``_FRAGMENT_CHARS`` 的分点（整句分点通常不会这么短）；
    2. 把分点用「。」拼起来**不等于**整体段落——按逗号切过的分点是拼不回原段的。
    两条都不满足时返回 ``None``，表示「无需修复」。
    """
    narrative = (narrative or "").strip()
    cleaned = [p.strip() for p in (points or []) if p and p.strip()]
    if not narrative or not cleaned:
        return None
    if not any(len(p) < _FRAGMENT_CHARS for p in cleaned):
        return None
    if "。".join(cleaned) == narrative:
        return None
    repaired = split_by_sentence(narrative)
    # 只有结果真的与现状不同才算「需要修复」：原段本来就只有一句时，重算成一条也是修复。
    return repaired if repaired and repaired != cleaned else None


class ProfileFact(BaseModel):
    text: str
    evidence_paths: list[str] = Field(default_factory=list)

    @field_validator("text", mode="before")
    @classmethod
    def _paragraph(cls, value):
        return collapse_to_paragraph(str(value or ""))


class ProfilePoint(BaseModel):
    text: str
    evidence_paths: list[str] = Field(default_factory=list)

    @field_validator("text", mode="before")
    @classmethod
    def _paragraph(cls, value):
        return collapse_to_paragraph(str(value or ""))


class ProfilePair(BaseModel):
    facts: list[ProfileFact] = Field(default_factory=list)
    narrative: str = ""
    points: list[ProfilePoint] = Field(default_factory=list)
    compact: str = ""

    @field_validator("compact", mode="before")
    @classmethod
    def _strip(cls, value):
        return str(value or "").strip()

    @field_validator("narrative", mode="before")
    @classmethod
    def _paragraph(cls, value):
        # 形态不变量：narrative 恒为整段、无换行（供 embedding 与人工阅读）。
        return collapse_to_paragraph(str(value or ""))

    def key_facts(self) -> list[str]:
        return [f.text.strip() for f in self.facts if f.text.strip()]

    def is_consistent(self) -> bool:
        """关键事实必须同时出现在 narrative 与 points 文本中（子串级可判定）。"""
        narrative = self.narrative
        points_text = "\n".join(p.text for p in self.points)
        for fact in self.key_facts():
            if fact not in narrative and fact not in points_text:
                return False
            if fact not in narrative or fact not in points_text:
                return False
        return True


def collapse_to_paragraph(text: str) -> str:
    """把可能带换行的文本归一为「整段、无换行」。

    换行是形态而不是内容：这里按句读把相邻行拼回一段——上一行已以句末标点收尾就直接相接，
    否则补一个「。」，避免拼出「…经验负责交易系统」这类无法阅读的串。
    """
    paragraph = ""
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if paragraph and not paragraph.endswith(_CLAUSE_TERMINATORS):
            paragraph += "。"
        paragraph += line
    return paragraph


def build_profile_pair(narrative: str) -> ProfilePair:
    """确定性回退：把单段文本按句读拆点，事实取每点首句。

    只按换行拆点会把「整段无换行」的画像退化成单个分点，双形态名存实亡；
    因此这里与人工编辑路径共用 ``split_profile_clauses`` 的同一套切分口径。
    """
    narrative = (narrative or "").strip()
    if not narrative:
        return ProfilePair()
    clauses = split_profile_clauses(narrative) or [narrative]
    points = [ProfilePoint(text=clause, evidence_paths=[]) for clause in clauses]
    facts = [
        ProfileFact(text=p.text[:80], evidence_paths=p.evidence_paths)
        for p in points
    ]
    compact = clauses[0][:60]
    return ProfilePair(facts=facts, narrative=narrative, points=points, compact=compact)


def pair_from_points(points, *, compact: str = "") -> ProfilePair:
    """以分点为唯一事实源派生双形态：整段按分点顺序拼接，逐字同源天然成立。

    模型只产出分点时用它补齐 narrative / facts / compact，省掉「同一份事实写两遍」
    与随之而来的逐字自检；空分点返回空画像，由调用方决定回退。
    """
    usable = [
        ProfilePoint(text=str(getattr(p, "text", "") or "").strip(),
                     evidence_paths=list(getattr(p, "evidence_paths", ()) or ()))
        for p in (points or [])
    ]
    usable = [p for p in usable if p.text]
    if not usable:
        return ProfilePair()
    return ProfilePair(
        facts=[ProfileFact(text=p.text, evidence_paths=list(p.evidence_paths)) for p in usable],
        narrative=join_points_as_narrative(usable),
        points=usable,
        compact=(compact or "").strip() or usable[0].text[:60],
    )


def normalize_points(raw: object) -> list[dict]:
    """把外部透传的分点规整为 ``{"text", "evidence_paths"}`` 列表。

    用于重新生成链路：前端把模型产出的真分点原样回传保存，后端不再按标点伪拆点。
    非列表、空文本、非字典项一律丢弃；全为空时返回空列表，调用方据此回退到确定性拆点。
    """
    if not isinstance(raw, (list, tuple)):
        return []
    points: list[dict] = []
    for item in raw:
        if isinstance(item, ProfilePoint):
            text, paths = item.text, item.evidence_paths
        elif isinstance(item, dict):
            text = item.get("text")
            paths = item.get("evidence_paths")
        else:
            continue
        text = str(text or "").strip()
        if not text:
            continue
        evidence_paths = (
            [str(p).strip() for p in paths if str(p).strip()]
            if isinstance(paths, (list, tuple))
            else []
        )
        points.append({"text": text, "evidence_paths": evidence_paths})
    return points


def reconcile_pair(pair: ProfilePair) -> ProfilePair:
    """把模型产出的双形态对齐为「同源可判定」形态，保证两种形态都能落库。

    模型习惯把 facts 写成 points 的改写（如「张三具备5年后端经验的工程师」对「5年后端经验」），
    逐字匹配恒不成立；若据此判为不一致，画像永远无法落库。这里统一以分点为唯一事实源：
    分点文本同时构成整体段落与关键事实，整体段落仅在已逐字包含全部分点时保留模型原文。
    """
    points = [
        ProfilePoint(text=p.text.strip(), evidence_paths=list(p.evidence_paths))
        for p in pair.points if p.text.strip()
    ]
    if not points:
        # 模型没给出可用分点：退化为按句读拆点，同样保证两种形态同源。
        return build_profile_pair(pair.narrative)
    if pair.is_consistent():
        return pair
    if any(p.text not in pair.narrative for p in points):
        # 分点已不在整段里：整段作废，以分点为唯一事实源重建（与「模型只给分点」共用同一套派生口径）。
        return pair_from_points(points, compact=pair.compact)
    return ProfilePair(
        facts=[ProfileFact(text=p.text, evidence_paths=list(p.evidence_paths)) for p in points],
        narrative=pair.narrative,
        points=points,
        compact=pair.compact or points[0].text[:60],
    )


def join_points_as_narrative(points: list[ProfilePoint]) -> str:
    """把分点按顺序拼成「整段、无换行」的 narrative，同时保留逐字可判定的同源关系。

    分点文本原样保留（不做 rstrip，否则分点不再是 narrative 的子串，一致性判定失效）；
    只在分点自身没有句末标点收尾时补一个「。」，避免拼出「…经验负责交易系统」这类无法阅读的串。
    """
    narrative = ""
    for point in points:
        if narrative and not narrative.endswith(_CLAUSE_TERMINATORS):
            narrative += "。"
        narrative += point.text
    return narrative


def repaired_profile_view(parsed_data: dict | None) -> dict | None:
    """返回用于**展示**的 `parsed_data` 视图：把历史遗留的碎片分点按整体段落重算。

    只改 `ai_profile_points`（纯展示字段），不动 `ai_profile_summary` / `ai_profile_narrative`，
    也不写库——`repair_profile_points` 判定为「无需修复」时原样返回同一个 dict。
    落库修复（连同向量索引重建）由 `scripts/repair_profile_points_2026_09_22.py` 单独做。

    放在后端而不是前端：切分口径只能有一份实现，否则前后端会各自漂移。
    """
    if not parsed_data:
        return parsed_data
    points = [
        str(item.get("text") or "") if isinstance(item, dict) else str(item)
        for item in (parsed_data.get("ai_profile_points") or [])
    ]
    narrative = str(
        parsed_data.get("ai_profile_narrative") or parsed_data.get("ai_profile_summary") or ""
    )
    repaired = repair_profile_points(narrative, points)
    if repaired is None:
        return parsed_data
    view = dict(parsed_data)
    # 证据路径无法跨切分口径映射，展示视图里留空；真实证据仍保存在 revision 里。
    view["ai_profile_points"] = [
        {"text": text, "evidence_paths": []} for text in repaired
    ]
    return view


def split_profile_clauses(narrative: str) -> list[str]:
    """把一段人工画像按句读切为真正的分点，不增删事实。

    只按**句末标点**切分（见 `_SENTENCE_BREAK`）。「，」「、」是句内停顿，按它们切会让
    画像在界面上变成「一词一行」——这正是本次要修的展示问题。
    """
    return split_by_sentence(narrative)


async def produce_pair_with_vet(
    produce: Callable[[str | None, str | None], Awaitable[ProfilePair]],
    *,
    side: str,
    logger: logging.Logger | None = None,
    max_repairs: int = 1,
    budget_seconds: float | None = REPAIR_BUDGET_SECONDS,
    on_stage: Callable[[str], None] | None = None,
) -> ProfilePair:
    """产出画像，并在校验不通过时带着违规原因做定点重写。

    调用次数被两把闸门同时压住，因为「重新生成画像」是**交互式**入口，最坏情况必须是
    有界的（原来最坏 3 次调用，每次都可能几十秒，表现就是一直转圈）：

    - ``max_repairs``（默认 1）：总调用数 ≤ 2。原先默认 2 且对「仅超字数」再放一次，
      所以最坏 3 次；实测超字数多半是模型口径偏好，再问一次也很少真的修好。
    - ``budget_seconds``（默认 90）：初稿 + 重写共用的墙钟预算。**只用来决定要不要
      再发起一次重写**，不能中断已经在飞的那次调用（那由 provider 的读超时兜住）。
      超预算就返回当前最好版本——宁可给一版能用的画像，也不要继续重写。

    任何一次没有改善就停手并保留更好的版本：校验是产出前的护栏，不是无限重试的优化器。

    ``on_stage`` 是**同步**进度回调，用于交互式入口的阶段性反馈：``draft``（正在出初稿）、
    ``repair``（初稿未过校验，正在定向重写）。只在真的还有第二次调用时才发 ``repair``，
    所以界面能直接看出「会不会再等一分钟」。同步回调是为了不影响异步链路——调用方
    只做 `queue.put_nowait` 这类不会失败的动作。
    """
    log = logger or logging.getLogger(__name__)

    def _stage(name: str) -> None:
        if on_stage is not None:
            on_stage(name)

    # `0` 是「一次重写都不许」的有意义取值，所以判空必须用 `is not None`。
    deadline = time.monotonic() + budget_seconds if budget_seconds is not None else None
    _stage("draft")
    best = await produce(None, None)
    best_issues = profile_spec.vet_profile(best.narrative, side=side)
    for _ in range(max_repairs):
        if not best_issues:
            break
        if deadline is not None and time.monotonic() >= deadline:
            log.warning("画像重写预算已用尽（%.0fs），返回当前版本：side=%s issues=%s",
                        budget_seconds, side, "；".join(best_issues))
            break
        log.info("画像未通过校验（第 %d 次修正）：side=%s issues=%s",
                 1, side, "；".join(best_issues))
        _stage("repair")
        fixed = await produce(
            profile_spec.render_rewrite_instruction(best_issues, side=side, draft=best.narrative), None)
        fixed_issues = profile_spec.vet_profile(fixed.narrative, side=side)
        if profile_spec.issue_severity(fixed_issues) >= profile_spec.issue_severity(best_issues):
            log.warning("画像修正后未改善（%s → %s），保留更好的版本：side=%s issues=%s",
                        profile_spec.issue_severity(best_issues), profile_spec.issue_severity(fixed_issues),
                        side, "；".join(fixed_issues))
            break
        log.info("画像修正后改善：%s → %s side=%s",
                 profile_spec.issue_severity(best_issues), profile_spec.issue_severity(fixed_issues), side)
        best, best_issues = fixed, fixed_issues
    return best
