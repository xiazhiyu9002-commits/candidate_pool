"""学校名称归一化与映射查询，不依赖 HTTP。

用于把简历中的学校名映射到 school 表（标准名/别名 -> 标签/QS），
以及解析预览（不写入候选人）。"""
from __future__ import annotations

import re
import unicodedata

from sqlalchemy import select
from sqlalchemy.orm import Session

from kerui_recruit.db.models import School


def normalize_school_name(name: str | None) -> str | None:
    """Unicode NFKC 归一化 + 去空白/常见标点 + 英文小写。"""
    if not name:
        return None
    normalized = unicodedata.normalize("NFKC", str(name))
    normalized = re.sub(r"[\s\u3000\-—–()（）【】\[\]「」\"'，。,.·]", "", normalized)
    normalized = normalized.casefold()
    return normalized or None


class SchoolReference:
    def __init__(self, session_factory) -> None:
        self.session_factory = session_factory

    def alias_groups(self) -> dict[str, tuple[str, ...]]:
        """返回 canonical 标准名 -> 别名组的快照（含内置与用户导入的别名）。

        文档构建与查询概念解析都注入同一份快照，保证两侧学校别名不分化。
        """
        with self.session_factory() as session:
            schools = session.scalars(select(School)).all()
        result: dict[str, tuple[str, ...]] = {}
        for school in schools:
            result[school.canonical_name] = tuple(school.aliases or ())
        return result

    def resolve(self, name: str | None) -> dict:
        """返回学校映射结果；未识别时返回待映射标记，不猜测等级。"""
        normalized = normalize_school_name(name)
        if not normalized:
            return {"input": name, "matched": False, "status": "empty"}
        with self.session_factory() as session:
            school = session.scalar(
                select(School).where(School.normalized_name == normalized)
            )
            if school is None:
                # 别名匹配：别名按逗号/顿号拆分后归一化比对。
                rows = session.scalars(select(School)).all()
                for row in rows:
                    aliases = row.aliases or []
                    if any(normalize_school_name(alias) == normalized for alias in aliases):
                        school = row
                        break
        if school is None:
            return {"input": name, "matched": False, "status": "待映射"}
        return {
            "input": name,
            "matched": True,
            "canonical_name": school.canonical_name,
            "tags": school.tags or [],
            "qs_year": school.qs_year,
            "qs_rank_start": school.qs_rank_start,
            "qs_rank_end": school.qs_rank_end,
            "rank_display": school.rank_display,
        }


def recompute_educations(session_factory, educations: list) -> list:
    """按学校名重新计算教育经历的 school_tags / qs_rank 等派生属性。

    学校名变化后，旧的 school_tags / qs_rank / school_level 不再可信；必须根据
    当前学校名重新查映射表。未收录的学校不得继承旧标签与排名。
    """
    reference = SchoolReference(session_factory)
    result: list = []
    for edu in educations:
        if not isinstance(edu, dict):
            result.append(edu)
            continue
        school = edu.get("school")
        new_edu = dict(edu)
        resolved = reference.resolve(school)
        if resolved.get("matched"):
            new_edu["school_tags"] = list(resolved.get("tags") or [])
            if resolved.get("qs_rank_start") is not None:
                new_edu["qs_rank"] = resolved["qs_rank_start"]
            else:
                new_edu.pop("qs_rank", None)
            if resolved.get("qs_year") is not None:
                new_edu["qs_year"] = resolved["qs_year"]
            else:
                new_edu.pop("qs_year", None)
            if resolved.get("rank_display"):
                new_edu["rank_display"] = resolved["rank_display"]
            else:
                new_edu.pop("rank_display", None)
        else:
            # 学校未收录：清除旧派生属性，避免继承上一所学校的标签/排名。
            for key in ("school_tags", "qs_rank", "qs_year", "rank_display"):
                new_edu.pop(key, None)
        result.append(new_edu)
    return result
