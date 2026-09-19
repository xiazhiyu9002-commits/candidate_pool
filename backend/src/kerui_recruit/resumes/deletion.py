from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session, sessionmaker

from kerui_recruit.db.models import (
    Candidate,
    CandidateContact,
    CandidateJobCase,
    CorrectionLog,
    Employee,
    IndexSyncRecord,
    MatchResult,
    ResumeDocument,
    ResumeImportClaim,
    ResumeRevision,
    TaskEvent,
    TaskRecord,
)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class CandidateDeletionService:
    """物理删除候选人：流程历史通过快照保留，候选人与简历数据永久清除。

    数据库事务提交后，物理文件与搜索索引的清理通过幂等、可重试的后台任务完成，
    避免数据库回滚后文件已经丢失，也避免索引/文件清理失败后永久残留。
    """

    def __init__(self, session_factory: sessionmaker[Session], index=None,
                 *, blob_store=None, task_repository=None) -> None:
        self.session_factory = session_factory
        self.index = index
        self.blob_store = blob_store
        self.task_repository = task_repository

    def delete(self, candidate_id: str) -> bool:
        orphaned_paths: list[str] = []
        with self.session_factory() as session, session.begin():
            candidate = session.get(Candidate, candidate_id)
            if candidate is None:
                return False

            contact = session.scalar(
                select(CandidateContact).where(CandidateContact.candidate_id == candidate_id)
            )
            revisions = list(
                session.scalars(
                    select(ResumeRevision)
                    .join(ResumeDocument, ResumeRevision.document_id == ResumeDocument.id)
                    .where(ResumeDocument.candidate_id == candidate_id)
                )
            )
            profile = revisions[0].parsed_data if revisions else None
            blobs = [revision.blob for revision in revisions if revision.blob is not None]

            # 1. 流程快照并解除外键关联（保留轮次、事件与备注）。
            for case in session.scalars(
                select(CandidateJobCase).where(CandidateJobCase.candidate_id == candidate_id)
            ):
                case.candidate_name_snapshot = candidate.display_name
                case.candidate_phone_snapshot_encrypted = contact.phone_encrypted if contact else None
                case.candidate_email_snapshot_encrypted = contact.email_encrypted if contact else None
                case.candidate_profile_snapshot = profile
                case.candidate_deleted_at = _now()
                case.candidate_id = None

            # 2. 解除组织员工与候选人的绑定。
            session.execute(
                update(Employee).where(Employee.candidate_id == candidate_id).values(candidate_id=None)
            )

            # 3. 删除匹配结果、纠错记录、索引同步与导入幂等声明。
            session.execute(delete(MatchResult).where(MatchResult.candidate_id == candidate_id))
            session.execute(
                delete(CorrectionLog).where(
                    CorrectionLog.entity_type == "candidate", CorrectionLog.entity_id == candidate_id
                )
            )
            session.execute(
                delete(IndexSyncRecord).where(
                    IndexSyncRecord.entity_type == "candidate", IndexSyncRecord.entity_id == candidate_id
                )
            )
            session.execute(delete(ResumeImportClaim).where(ResumeImportClaim.candidate_id == candidate_id))

            # 4. Blob 引用计数递减；归零的 Blob 记录其物理路径供后台清理。
            for blob in blobs:
                blob.reference_count = max(0, blob.reference_count - 1)
            orphaned_paths = [blob.storage_path for blob in blobs if blob.reference_count <= 0]

            # 5. 删除候选人（级联删除联系方式、简历文档与版本）。
            session.delete(candidate)
            session.flush()

            for blob in blobs:
                if blob.reference_count <= 0:
                    session.delete(blob)

            # 6. 同一事务内注册幂等清理任务（物理文件），保证数据库删除与清理记录一致。
            self._enqueue_cleanup(session, candidate_id, orphaned_paths)

        # 7. 事务提交后同步清理索引实体（与 JD 删除一致，避免异步任务残留孤儿 chunk）。
        if self.index is not None:
            self.index.delete_candidate(candidate_id)
        return True

    def _enqueue_cleanup(self, session: Session, candidate_id: str, storage_paths: list[str]) -> None:
        """在删除事务内登记清理任务；任务随事务提交，避免出现无清理记录的孤立文件。"""
        if self.task_repository is None:
            # 无任务系统时立即幂等清理物理文件（索引已在 delete() 事务提交后同步清理）。
            if self.blob_store is not None:
                for path in storage_paths:
                    (self.blob_store.root / path).unlink(missing_ok=True)
            return
        idempotency_key = f"blob-cleanup:{candidate_id}"
        existing = session.scalar(
            select(TaskRecord).where(TaskRecord.idempotency_key == idempotency_key)
        )
        if existing is not None:
            return
        task = TaskRecord(
            task_type="BLOB_CLEANUP",
            queue_name="batch",
            priority=5,
            status="QUEUED",
            payload={"candidate_id": candidate_id, "storage_paths": list(dict.fromkeys(storage_paths))},
            idempotency_key=idempotency_key,
            max_attempts=5,
        )
        task.events.append(TaskEvent(from_status="PENDING", to_status="QUEUED"))
        session.add(task)
        session.flush()
