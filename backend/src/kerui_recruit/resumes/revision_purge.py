"""版本级硬删除：把同一候选人被取代的旧简历版本物理清除（只保留当前版本）。

与候选人级删除（``resumes/deletion.py``）分开：这里只删**版本**及其独占资源，
候选人本身与该候选人的联系方式、流程、备注继续存在。

删除前必须处理的引用——它们全是无外键的裸字符串，数据库不会报错但会静默失效：

- ``ResumeImportClaim.revision_id``：悬空后再次导入同一文件会命中「已导入」短路，
  返回一个**已死的 revision_id**，因此必须改指保留下来的版本。
- ``TaskRecord.payload``：指向被删版本的未终态任务必须取消，否则 worker 领到后在
  ``pipeline.run`` 里抛 ``LookupError``，重试满 ``max_attempts`` 才进死信。
- ``MatchResult.resume_revision_id`` / ``SearchReview.revision_id``：改指保留下来的
  版本，避免位置字段静默丢失、以及 AI 深度复核拿到空简历而被误判为不推荐。
- ``Blob``：引用计数递减，归零的物理文件交由 ``BLOB_CLEANUP`` 任务清理；同内容文件
  共用同一 Blob，因此绝不能直接归零。

搜索索引**不需要**在这里单独处理：``replace_candidate`` 会先删光该候选人的 chunk
再按当前版本重建，调用方（``_persist_ready``）已经 ``enqueue_sync``。
"""
from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from kerui_recruit.db.models import (
    MatchResult,
    ResumeDocument,
    ResumeImportClaim,
    ResumeRevision,
    SearchReview,
)
from kerui_recruit.resumes.deletion import cancel_pending_tasks, enqueue_blob_cleanup


def inherit_manual_overrides(
    session: Session, *, candidate_id: str, keep_revision_id: str
) -> dict:
    """收集该候选人其它版本上的人工修订，供新版本继承（决策 D7）。

    逐字段按版本创建时间升序合并：更新的版本覆盖更早的版本。返回空 dict 表示过去
    没有人工修订，调用方应保持原样。
    """
    revisions = session.scalars(
        select(ResumeRevision)
        .join(ResumeDocument, ResumeDocument.id == ResumeRevision.document_id)
        .where(
            ResumeDocument.candidate_id == candidate_id,
            ResumeRevision.id != keep_revision_id,
        )
        .order_by(ResumeRevision.created_at.asc())
    ).all()
    inherited: dict = {}
    for revision in revisions:
        inherited.update(dict(revision.manual_overrides or {}))
    return inherited


def purge_superseded_revisions(
    session: Session,
    *,
    candidate_id: str,
    keep_revision_id: str,
    blob_store=None,
    task_repository=None,
) -> list[str]:
    """硬删除该候选人名下除 ``keep_revision_id`` 外的所有版本，返回被删版本 id。

    必须在**同一个事务**内与「新版本置为 READY、候选人列改写」一起提交，否则中途
    失败会留下「旧版本已删、新版本仍不可用」的空洞状态。
    """
    revisions = list(session.scalars(
        select(ResumeRevision)
        .join(ResumeDocument, ResumeDocument.id == ResumeRevision.document_id)
        .where(
            ResumeDocument.candidate_id == candidate_id,
            ResumeRevision.id != keep_revision_id,
        )
    ).all())
    if not revisions:
        return []
    removed_ids = [revision.id for revision in revisions]

    # 导入声明改指保留下来的版本，避免「再次导入同一文件 → 返回已死 revision_id」。
    session.execute(
        update(ResumeImportClaim)
        .where(ResumeImportClaim.revision_id.in_(removed_ids))
        .values(revision_id=keep_revision_id)
    )
    # 历史匹配/复核结果改指保留下来的版本：分数仍是历史快照，但引用不再悬空。
    session.execute(
        update(MatchResult)
        .where(MatchResult.resume_revision_id.in_(removed_ids))
        .values(resume_revision_id=keep_revision_id)
    )
    session.execute(
        update(SearchReview)
        .where(SearchReview.revision_id.in_(removed_ids))
        .values(revision_id=keep_revision_id)
    )
    # 指向被删版本的未终态任务：新版本已解析完成，这些活不再需要干。
    cancel_pending_tasks(session, set(removed_ids))

    blobs = {revision.blob_id: revision.blob for revision in revisions if revision.blob_id}
    for blob in blobs.values():
        blob.reference_count = max(0, blob.reference_count - 1)
    orphaned_paths = [blob.storage_path for blob in blobs.values() if blob.reference_count <= 0]

    # 必须先删版本、再删 Blob：反过来的话，仍引用该 Blob 的版本会在 flush 时被
    # 置空 blob_id（NOT NULL）而报错。
    for revision in revisions:
        session.delete(revision)
    session.flush()
    for blob in blobs.values():
        if blob.reference_count <= 0:
            session.delete(blob)

    # 版本级回执：payload 不带 candidate_id，否则清理处理器会连带删除该候选人的索引实体。
    enqueue_blob_cleanup(
        session,
        idempotency_key=f"blob-cleanup:revision:{keep_revision_id}",
        payload={},
        storage_paths=orphaned_paths,
        blob_store=blob_store,
        task_repository=task_repository,
    )

    session.flush()
    _prune_empty_documents(session, candidate_id)
    return removed_ids


def _prune_empty_documents(session: Session, candidate_id: str) -> None:
    """删除因版本被清理而变空的简历文档，避免留下孤儿行。"""
    documents = session.scalars(
        select(ResumeDocument).where(ResumeDocument.candidate_id == candidate_id)
    ).all()
    for document in documents:
        remaining = session.scalar(
            select(ResumeRevision.id).where(ResumeRevision.document_id == document.id).limit(1)
        )
        if remaining is None:
            session.delete(document)
