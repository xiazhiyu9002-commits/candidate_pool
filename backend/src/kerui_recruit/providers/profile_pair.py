"""画像双形态结构化契约：一次生成共享 facts + narrative + points + compact。

一致性要求：每个关键事实必须同时体现在 narrative 与 points 两种展示形态中；
任一关键事实缺失即判为不一致（待核），调用方不得把旧整体/新分点混在一起保留。
"""
from __future__ import annotations

import re

from pydantic import BaseModel, Field, field_validator

# 句末标点：分点自身已以此收尾时，拼接整体段落不再补「。」。
_CLAUSE_TERMINATORS = ("。", "！", "？", "；", "…", "，", ".", "!", "?", ";", ",")


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
    narrative = pair.narrative
    if any(p.text not in narrative for p in points):
        narrative = join_points_as_narrative(points)
    return ProfilePair(
        facts=[ProfileFact(text=p.text, evidence_paths=list(p.evidence_paths)) for p in points],
        narrative=narrative,
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


def split_profile_clauses(narrative: str) -> list[str]:
    """把一段人工画像按句读/逗号切为真正的分点，不增删事实。

    用于人工编辑后生成分点形态：单段文本也会被拆成多句，而不是整段作为一个点。
    逗号「、」不切分（技能/枚举仍归同一要点）。
    """
    parts = re.split(r"[。！？；;，,\n]+", narrative or "")
    return [p.strip() for p in parts if p.strip()]
