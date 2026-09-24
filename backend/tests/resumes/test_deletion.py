from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from kerui_recruit.db.migrate import migrate
from kerui_recruit.db.models import (
    Blob,
    Candidate,
    CandidateContact,
    CandidateJobCase,
    Jd,
    ResumeDocument,
    ResumeRevision,
    TaskRecord,
)
from kerui_recruit.db.session import create_engine_for
from kerui_recruit.resumes.deletion import CandidateDeletionService
from kerui_recruit.storage.blobs import BlobStore
from kerui_recruit.tasks.repository import TaskRepository, TaskSpec


def _factory(database: Path) -> sessionmaker[Session]:
    engine = create_engine_for(database)
    migrate(engine)
    return sessionmaker(engine)


def test_delete_preserves_case_with_snapshot(tmp_path: Path) -> None:
    factory = _factory(tmp_path / "recruit.sqlite3")
    with factory() as session:
        blob = Blob(content_sha256="a" * 64, suffix=".pdf", size_bytes=128,
                    storage_path="aa/aa/" + "a" * 64 + ".pdf", reference_count=1)
        candidate = Candidate(display_name="张三", total_years=Decimal("5.0"))
        document = ResumeDocument(candidate=candidate)
        revision = ResumeRevision(document=document, blob=blob, content_sha256=blob.content_sha256,
                                  original_filename="张三.pdf", status="READY",
                                  parsed_data={"ai_profile_summary": "画像"})
        contact = CandidateContact(candidate=candidate, phone_encrypted="cipher-phone",
                                   email_encrypted="cipher-email")
        jd = Jd(company="某公司", title="工程师", status="OPEN")
        case = CandidateJobCase(candidate=candidate, jd=jd, stage="待评估")
        session.add_all([candidate, jd, case, blob, document, revision, contact])
        session.commit()
        candidate_id = candidate.id
        case_id = case.id
        revision_id = revision.id
        blob_id = blob.id

    CandidateDeletionService(factory).delete(candidate_id)

    with factory() as session:
        assert session.get(Candidate, candidate_id) is None
        case = session.get(CandidateJobCase, case_id)
        assert case is not None
        assert case.candidate_id is None
        assert case.candidate_name_snapshot == "张三"
        assert case.candidate_phone_snapshot_encrypted == "cipher-phone"
        assert case.candidate_deleted_at is not None
        # 简历版本与候选人已清除，Blob 引用归零。
        assert session.get(ResumeRevision, revision_id) is None
        assert session.get(Blob, blob_id) is None


def test_delete_returns_false_for_unknown_candidate(tmp_path: Path) -> None:
    factory = _factory(tmp_path / "recruit.sqlite3")
    assert CandidateDeletionService(factory).delete("missing") is False


def test_delete_removes_physical_file_and_is_idempotent(tmp_path: Path) -> None:
    factory = _factory(tmp_path / "recruit.sqlite3")
    store = BlobStore(tmp_path / "blobs", tmp_path / "temp")
    storage_path = "aa/aa/" + "b" * 64 + ".pdf"
    target = store.root / storage_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"resume")

    with factory() as session:
        blob = Blob(content_sha256="b" * 64, suffix=".pdf", size_bytes=6,
                    storage_path=storage_path, reference_count=1)
        candidate = Candidate(display_name="张三")
        document = ResumeDocument(candidate=candidate)
        revision = ResumeRevision(document=document, blob=blob, content_sha256=blob.content_sha256,
                                  original_filename="张三.pdf", status="READY")
        session.add_all([candidate, blob, document, revision])
        session.commit()
        candidate_id = candidate.id

    CandidateDeletionService(factory, blob_store=store).delete(candidate_id)
    assert not target.exists()

    # 幂等：再次删除（文件已不存在）不抛异常。
    CandidateDeletionService(factory, blob_store=store).delete("missing")


def test_delete_enqueues_retryable_cleanup_task(tmp_path: Path) -> None:
    factory = _factory(tmp_path / "recruit.sqlite3")
    store = BlobStore(tmp_path / "blobs", tmp_path / "temp")
    storage_path = "cc/cc/" + "c" * 64 + ".pdf"
    (store.root / storage_path).parent.mkdir(parents=True, exist_ok=True)
    (store.root / storage_path).write_bytes(b"resume")

    with factory() as session:
        blob = Blob(content_sha256="c" * 64, suffix=".pdf", size_bytes=6,
                    storage_path=storage_path, reference_count=1)
        candidate = Candidate(display_name="张三")
        document = ResumeDocument(candidate=candidate)
        revision = ResumeRevision(document=document, blob=blob, content_sha256=blob.content_sha256,
                                  original_filename="张三.pdf", status="READY")
        session.add_all([candidate, blob, document, revision])
        session.commit()
        candidate_id = candidate.id

    from kerui_recruit.tasks.repository import TaskRepository
    repo = TaskRepository(factory)
    CandidateDeletionService(factory, blob_store=store, task_repository=repo).delete(candidate_id)

    # 清理任务必须随删除事务一并持久化，且幂等（同一候选人只登记一次）。
    with factory() as session:
        tasks = session.scalars(
            select(TaskRecord).where(TaskRecord.idempotency_key == f"blob-cleanup:{candidate_id}")
        ).all()
        assert len(tasks) == 1
        task = tasks[0]
        assert task.task_type == "BLOB_CLEANUP"
        assert task.payload["candidate_id"] == candidate_id
        assert task.payload["storage_paths"] == [storage_path]


def test_delete_cancels_pending_tasks_of_the_deleted_candidate(tmp_path: Path) -> None:
    """删除候选人要一并取消引用它的未终态任务，别让它们空转到死信。

    实测 `.dev-data` 128 条 DEAD_LETTER 里 **56 条**是「Resume revision not found」——
    候选人删了、队列里的 PARSE_RESUME 还会被领到、按 max_attempts 重试满 5 次才进死信。
    这既污染死信指标（原先据此误判成「解析质量差」），也白占 worker。
    """
    factory = _factory(tmp_path / "recruit.sqlite3")
    with factory() as session:
        blob = Blob(content_sha256="d" * 64, suffix=".pdf", size_bytes=6,
                    storage_path="dd/dd/" + "d" * 64 + ".pdf", reference_count=1)
        candidate = Candidate(display_name="张三")
        document = ResumeDocument(candidate=candidate)
        revision = ResumeRevision(document=document, blob=blob, content_sha256=blob.content_sha256,
                                  original_filename="张三.pdf", status="READY")
        other_blob = Blob(content_sha256="e" * 64, suffix=".pdf", size_bytes=6,
                          storage_path="ee/ee/" + "e" * 64 + ".pdf", reference_count=1)
        other = Candidate(display_name="李四")
        other_document = ResumeDocument(candidate=other)
        other_revision = ResumeRevision(document=other_document, blob=other_blob,
                                        content_sha256=other_blob.content_sha256,
                                        original_filename="李四.pdf", status="READY")
        session.add_all([candidate, blob, document, revision,
                         other, other_blob, other_document, other_revision])
        session.commit()
        candidate_id, revision_id = candidate.id, revision.id
        other_revision_id = other_revision.id

    repo = TaskRepository(factory)
    mine = repo.enqueue(TaskSpec("PARSE_RESUME", "normal", 10,
                                 {"revision_id": revision_id}, "parse:mine"))
    # 嵌套在 list 里的载荷也要能匹配上（不同任务类型的载荷结构不同）。
    mine_nested = repo.enqueue(TaskSpec("REPARSE", "normal", 10,
                                        {"ids": [candidate_id]}, "reparse:mine"))
    theirs = repo.enqueue(TaskSpec("PARSE_RESUME", "normal", 10,
                                   {"revision_id": other_revision_id}, "parse:theirs"))

    CandidateDeletionService(factory).delete(candidate_id)

    with factory() as session:
        assert session.get(TaskRecord, mine).status == "CANCELLED"
        assert session.get(TaskRecord, mine_nested).status == "CANCELLED"
        # 别的候选人的任务不能受影响。
        assert session.get(TaskRecord, theirs).status == "QUEUED"
