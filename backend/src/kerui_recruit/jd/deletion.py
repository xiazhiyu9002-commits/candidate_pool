from __future__ import annotations

from sqlalchemy import delete
from sqlalchemy.orm import Session, sessionmaker

from kerui_recruit.db.models import CorrectionLog, IndexSyncRecord, Jd, MatchResult


class JdDeletionService:
    """物理删除岗位：永久清除 JD 及其版本、要求、匹配结果与索引。

    岗位相关的流程（CandidateJobCase）通过 ``jd_id`` 外键 ``ondelete=CASCADE``
    自动级联删除；MatchRun 通过 ``jd_revision_id`` 外键 ``ondelete=SET NULL``
    自动解除关联。匹配结果与纠错记录为普通字段引用，需在此显式删除。
    """

    def __init__(self, session_factory: sessionmaker[Session], jd_index=None) -> None:
        self.session_factory = session_factory
        self.jd_index = jd_index

    def delete(self, jd_id: str) -> bool:
        with self.session_factory() as session, session.begin():
            jd = session.get(Jd, jd_id)
            if jd is None:
                return False
            revision_ids = [revision.id for revision in jd.revisions]
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
            # 级联删除 JdRevision、JdRequirement 与 CandidateJobCase（外键 CASCADE）。
            session.delete(jd)
        if self.jd_index is not None:
            self.jd_index.delete_jd(jd_id)
        return True
