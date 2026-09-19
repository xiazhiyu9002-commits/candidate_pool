"""统一多份当前简历的人选证据视图。

一个候选人可能有多份「当前」READY 修订。资格判断与展示必须使用同一份视图：
- 硬字段（方向、年限、学历、地点等）取最近修订；
- 技能 / 经历 / 项目跨修订去重合并，保留来源修订 ID。
"""
from __future__ import annotations

import json
from datetime import datetime

from kerui_recruit.direction.policy import extract_multi_directions
from kerui_recruit.search.query import normalize_skill


def _sorted_key(revision) -> tuple:
    created = getattr(revision, "created_at", None)
    rid = getattr(revision, "id", None)
    return (created or datetime.min.replace(tzinfo=None), rid or "")


def _dedup_key(item: dict) -> str:
    return json.dumps({k: item.get(k) for k in ("summary", "title", "name", "tech_stack", "company")},
                      ensure_ascii=False, sort_keys=True)


def _merge_items(revisions, field: str) -> list[dict]:
    merged: list[dict] = []
    seen: set[str] = set()
    for revision in revisions:
        data = revision.parsed_data or {}
        for item in data.get(field) or []:
            if not isinstance(item, dict):
                continue
            key = _dedup_key(item)
            if key not in seen:
                seen.add(key)
                merged.append(item)
    return merged


def build_candidate_view(revisions, candidate) -> dict:
    """合并候选人全部当前修订为一致的人选证据视图。"""
    if not revisions:
        return {}

    ordered = sorted(revisions, key=_sorted_key, reverse=True)
    latest = dict(ordered[0].parsed_data or {})

    # 技能去重合并（casefold 归一后去重）。
    skills: list[str] = []
    seen_skills: set[str] = set()
    for revision in ordered:
        for skill in (revision.parsed_data or {}).get("skills") or []:
            if not skill:
                continue
            key = normalize_skill(str(skill)).casefold()
            if key and key not in seen_skills:
                seen_skills.add(key)
                skills.append(skill)

    view = dict(latest)
    view["skills"] = skills
    view["experiences"] = _merge_items(ordered, "experiences")
    view["projects"] = _merge_items(ordered, "projects")
    view["_source_revision_ids"] = [revision.id for revision in ordered]

    # 多值方向同为硬字段：取最近修订，缺省回退 direction_assessment / 单值 direction。
    career, specs, business = extract_multi_directions(latest)
    view["career_directions"] = list(career)
    view["career_specializations"] = list(specs)
    view["business_directions"] = list(business)

    # 候选人表上的权威硬字段兜底（人工编辑的年限/学历优先于解析值）。
    if candidate is not None:
        if getattr(candidate, "highest_degree", None) and not view.get("highest_degree"):
            view["highest_degree"] = candidate.highest_degree
        total_years = getattr(candidate, "total_years", None)
        if total_years is not None and view.get("total_years") is None:
            view["total_years"] = float(total_years)

    return view
