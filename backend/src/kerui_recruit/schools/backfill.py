"""学校映射存量回填：为已有候选人的教育经历补齐标签与 QS 排名字段。

幂等：只补缺失字段，不覆盖人工修改；处理完后为候选人重新入队索引同步。
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from kerui_recruit.db.models import Candidate, ResumeDocument, ResumeRevision, School
from kerui_recruit.schools.reference import normalize_school_name
from kerui_recruit.search.sync import enqueue_sync

_SCHOOL_LEVEL_PRIORITY = {"985": 4, "211": 3, "双一流": 2, "海外": 1, "普通": 0}


def _build_index(session: Session) -> dict[str, School]:
    index: dict[str, School] = {}
    for school in session.scalars(select(School)).all():
        index[school.normalized_name] = school
        for alias in school.aliases or []:
            normalized = normalize_school_name(alias)
            if normalized:
                index.setdefault(normalized, school)
    return index


def _highest_level(tags: list[str]) -> str | None:
    best: str | None = None
    best_priority = -1
    for tag in tags:
        priority = _SCHOOL_LEVEL_PRIORITY.get(tag, -1)
        if priority > best_priority:
            best, best_priority = tag, priority
    return best


def backfill_school_mappings(session_factory: sessionmaker[Session], report=None) -> dict:
    total = 0
    updated = 0
    skipped = 0
    with session_factory() as session:
        index = _build_index(session)
        revisions = list(session.scalars(
            select(ResumeRevision)
            .join(ResumeDocument, ResumeDocument.id == ResumeRevision.document_id)
            .join(Candidate, Candidate.id == ResumeDocument.candidate_id)
            .where(
                ResumeRevision.is_current.is_(True),
                ResumeRevision.status == "READY",
                Candidate.deleted_at.is_(None),
                Candidate.status.not_in(("ARCHIVED", "PENDING_REVIEW")),
            )
        ).all())
        total_count = len(revisions)
        for revision in revisions:
            total += 1
            data = dict(revision.parsed_data or {})
            overrides = revision.manual_overrides or {}
            changed = False

            educations = data.get("educations") or []
            for edu in educations:
                if not isinstance(edu, dict):
                    continue
                school = index.get(normalize_school_name(edu.get("school")))
                if school is None:
                    continue
                if not edu.get("school_tags"):
                    edu["school_tags"] = list(school.tags or [])
                    changed = True
                if edu.get("qs_year") is None and school.qs_year is not None:
                    edu["qs_year"] = school.qs_year
                    changed = True
                if edu.get("qs_rank") is None and school.qs_rank_start is not None:
                    edu["qs_rank"] = school.qs_rank_start
                    changed = True
                if not edu.get("rank_display") and school.rank_display:
                    edu["rank_display"] = school.rank_display
                    changed = True
            if educations:
                data["educations"] = educations

            # 兼容字段（最高标签 / 最优 QS）仅在未被人工覆盖时派生。
            if "school_level" not in overrides and "school" not in overrides:
                all_tags: list[str] = []
                for edu in educations:
                    if isinstance(edu, dict):
                        all_tags.extend(edu.get("school_tags") or [])
                level = _highest_level(all_tags)
                if level and not data.get("school_level"):
                    data["school_level"] = level
                    changed = True
            if "qs_rank" not in overrides and "school" not in overrides:
                ranks = [
                    edu.get("qs_rank")
                    for edu in educations
                    if isinstance(edu, dict) and edu.get("qs_rank")
                ]
                if ranks and data.get("qs_rank") is None:
                    data["qs_rank"] = min(ranks)
                    changed = True

            if changed:
                revision.parsed_data = data
                enqueue_sync(session, "candidate", revision.document.candidate_id)
                updated += 1
            else:
                skipped += 1
            # 每条提交一次并上报进度，避免整批结束后才一次性推进到 100%。
            session.commit()
            if report is not None:
                report(100 if total_count == 0 else int(total * 100 / total_count))
    return {"total": total, "updated": updated, "skipped": skipped}
