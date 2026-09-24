from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from kerui_recruit.db.models import (
    CandidateJobCase,
    CorrectionLog,
    IndexSyncRecord,
    Jd,
    MatchResult,
)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class JdDeletionService:
    """物理删除岗位：永久清除 JD 及其版本、要求、匹配结果与索引。

    岗位相关的流程（``CandidateJobCase``）**不再级联删除**：改为先写岗位快照
    （岗位名/公司/画像 + 删除时间）再把 ``jd_id`` 置空，流程、轮次、面试记录与备注全部保留
    —— 与候选人物理删除保持同一套语义。
    MatchRun 通过 ``jd_revision_id`` 外键 ``ondelete=SET NULL`` 自动解除关联。
    匹配结果与纠错记录为普通字段引用，需在此显式删除。
    """

    def __init__(self, session_factory: sessionmaker[Session], jd_index=None) -> None:
        self.session_factory = session_factory
        self.jd_index = jd_index

    def delete(self, jd_id: str) -> bool:
        with self.session_factory() as session, session.begin():
            jd = session.get(Jd, jd_id)
            if jd is None:
                return False
            revisions = list(jd.revisions)
            revision_ids = [revision.id for revision in revisions]
            profile = revisions[0].parsed_data if revisions else None

            # 1. 流程快照并解除外键关联（保留轮次、面试记录与备注）。
            for case in session.scalars(
                select(CandidateJobCase).where(CandidateJobCase.jd_id == jd_id)
            ):
                case.jd_title_snapshot = jd.title
                case.jd_company_snapshot = jd.company
                case.jd_profile_snapshot = profile
                case.jd_deleted_at = _now()
                case.jd_id = None

            if revision_ids:
                session.execute(
                    delete(MatchResult).where(MatchResult.jd_revision_id.in_(revision_ids))
                )
            session.execute(
                delete(CorrectionLog).where(
                    CorrectionLog.entity_type == "jd", CorrectionLog.entity_id == jd_id
                )
            )
            session.execute(
                delete(IndexSyncRecord).where(
                    IndexSyncRecord.entity_type == "jd", IndexSyncRecord.entity_id == jd_id
                )
            )
            # 级联删除 JdRevision 与 JdRequirement（ORM / 外键 CASCADE）。
            session.delete(jd)
        if self.jd_index is not None:
            self.jd_index.delete_jd(jd_id)
        return True
