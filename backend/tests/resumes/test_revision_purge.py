"""同一人更新简历：合并到既有候选人，并只保留新版本（旧版本物理删除）。

决策来源见 ``docs/superpowers/plans/2026-09-20-work-years-rollover-resume-update-and-todo.md``：
D5（手机号或邮箱任一命中即同一人）、D6（解析成功后才删旧版）、D7（重生成解析字段、
保留人工修订）、D8（旧版本硬删除、不用软删除）、D12（历史匹配结果改指新版本）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pymupdf
import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from kerui_recruit.db.migrate import migrate
from kerui_recruit.db.models import (
    Blob,
    Candidate,
    IndexSyncRecord,
    MatchResult,
    MatchRun,
    ResumeImportClaim,
    ResumeRevision,
    SearchReview,
    TaskRecord,
)
from kerui_recruit.db.session import create_engine_for
from kerui_recruit.encryption.service import EncryptionService
from kerui_recruit.providers.fakes import FakeEmbeddingProvider
from kerui_recruit.resumes.ingest import IngestResume, ResumeIngestService
from kerui_recruit.resumes.pipeline import ResumePipeline
from kerui_recruit.resumes.structured import ParsedExperience, ParsedResume
from kerui_recruit.search.degrees import normalize_degree
from kerui_recruit.storage.blobs import BlobStore
from kerui_recruit.tasks.repository import TaskRepository

_CONTACT_LINE = "Email: zhang@example.com\nPhone: 13800138000"


def _pdf(body: str) -> bytes:
    pdf = pymupdf.open()
    pdf.new_page().insert_text((72, 72), body)
    content = pdf.tobytes()
    pdf.close()
    return content


def _resume_text(marker: str) -> str:
    return f"Python Finance Resume {marker}\n{_CONTACT_LINE}"


class ContactParser:
    """按构造参数返回解析结果，用于模拟同一个人先后上传两份内容不同的简历。"""

    def __init__(self, *, years: int, degree: str = "硕士") -> None:
        self.years = years
        self.degree = degree

    async def parse_resume(self, text: str) -> ParsedResume:
        assert "Python Finance" in text
        return ParsedResume(
            name="张三",
            total_years=self.years,
            highest_degree=self.degree,
            skills=["Python", "金融风控"],
            summary="金融科技后端工程师",
            experiences=[
                ParsedExperience(company="示例科技", title="后端工程师", summary="负责 Python 风控平台")
            ],
        )


class ExplodingParser:
    """模拟解析失败（例如上游错误）：此时绝不能删除旧版本。"""

    async def parse_resume(self, text: str) -> ParsedResume:
        raise RuntimeError("provider exploded")


@dataclass
class _Harness:
    """真实 ingest + 真实 pipeline 的最小脚手架（含联系方式加密服务，否则指纹不落库）。"""

    factory: Any
    store: BlobStore
    encryption: EncryptionService

    def ingest(self, content: bytes, filename: str):
        with self.factory() as session:
            result = ResumeIngestService(session, self.store).ingest(
                IngestResume(filename=filename, content=content)
            )
            session.commit()
            return result

    def pipeline(self, parser) -> ResumePipeline:
        return ResumePipeline(
            session_factory=self.factory,
            blob_store=self.store,
            parser=parser,
            embedding_provider=FakeEmbeddingProvider(dimension=16),
            encryption_service=self.encryption,
            # 与生产一致：物理文件清理走可重试的后台任务，而不是立即删除。
            task_repository=TaskRepository(self.factory),
        )


@pytest.fixture()
def harness(tmp_path) -> _Harness:
    engine = create_engine_for(tmp_path / "db.sqlite3")
    migrate(engine)
    return _Harness(
        factory=sessionmaker(engine, expire_on_commit=False),
        store=BlobStore(tmp_path / "blobs", tmp_path / "temp"),
        encryption=EncryptionService(key_path=str(tmp_path / "encryption.key")),
    )


@pytest.mark.asyncio
async def test_reimport_from_the_same_person_keeps_only_the_new_revision(harness: _Harness) -> None:
    first = harness.ingest(_pdf(_resume_text("five years")), "first.pdf")
    await harness.pipeline(ContactParser(years=5)).run(first.revision_id)

    second = harness.ingest(_pdf(_resume_text("six years")), "second.pdf")
    await harness.pipeline(ContactParser(years=6)).run(second.revision_id)

    with harness.factory() as session:
        candidates = session.scalars(select(Candidate)).all()
        # 占位候选人是物理删除的：库里只剩真正的那个人。
        assert [c.id for c in candidates] == [first.candidate_id]
        assert candidates[0].display_name == "张三"
        # 以新版简历重新生成：年限被新解析结果覆盖。
        assert str(candidates[0].total_years) == "6.0"
        # 只保留当前版本。
        assert [r.id for r in session.scalars(select(ResumeRevision)).all()] == [second.revision_id]
        # 索引整体重建已入队（replace_candidate 会先删光该候选人的 chunk）。
        assert session.scalar(
            select(IndexSyncRecord).where(IndexSyncRecord.entity_id == first.candidate_id)
        ) is not None


@pytest.mark.asyncio
async def test_purge_releases_the_old_blob_without_touching_the_index_entity(harness: _Harness) -> None:
    first = harness.ingest(_pdf(_resume_text("five years")), "first.pdf")
    await harness.pipeline(ContactParser(years=5)).run(first.revision_id)
    second = harness.ingest(_pdf(_resume_text("six years")), "second.pdf")
    await harness.pipeline(ContactParser(years=6)).run(second.revision_id)

    with harness.factory() as session:
        # 旧文件的 Blob 引用归零后被删除，物理文件交给清理任务。
        assert len(session.scalars(select(Blob)).all()) == 1
        cleanup = session.scalars(
            select(TaskRecord).where(TaskRecord.task_type == "BLOB_CLEANUP")
        ).all()
        assert len(cleanup) == 1
        assert cleanup[0].payload["storage_paths"]
        # 版本级清理**不得**带 candidate_id，否则清理处理器会连带删除该候选人的索引实体。
        assert "candidate_id" not in cleanup[0].payload


@pytest.mark.asyncio
async def test_reimporting_the_superseded_file_never_returns_a_dead_revision(harness: _Harness) -> None:
    """导入声明必须改指新版本，否则再次导入同一文件会返回一个已死的 revision_id。"""
    first_content = _pdf(_resume_text("five years"))
    first = harness.ingest(first_content, "first.pdf")
    await harness.pipeline(ContactParser(years=5)).run(first.revision_id)
    second = harness.ingest(_pdf(_resume_text("six years")), "second.pdf")
    await harness.pipeline(ContactParser(years=6)).run(second.revision_id)

    reimport = harness.ingest(first_content, "first.pdf")

    assert reimport.action == "ALREADY_IMPORTED"
    assert reimport.candidate_id == first.candidate_id
    with harness.factory() as session:
        assert reimport.revision_id == second.revision_id
        # 关键：返回的 revision 必须真实存在（曾经会返回已死的 id）。
        assert session.get(ResumeRevision, reimport.revision_id) is not None
        claim = session.scalar(
            select(ResumeImportClaim).where(ResumeImportClaim.candidate_id == first.candidate_id)
        )
        assert claim is not None and claim.revision_id == second.revision_id


@pytest.mark.asyncio
async def test_manual_overrides_survive_the_reimport(harness: _Harness) -> None:
    """决策 D7：重生成解析字段，但人工改过的字段必须保留（否则删旧版本等于丢弃校正）。"""
    first = harness.ingest(_pdf(_resume_text("five years")), "first.pdf")
    await harness.pipeline(ContactParser(years=5)).run(first.revision_id)
    with harness.factory() as session, session.begin():
        revision = session.get(ResumeRevision, first.revision_id)
        revision.manual_overrides = {"highest_degree": "博士"}
        parsed = dict(revision.parsed_data)
        parsed["highest_degree"] = "博士"
        revision.parsed_data = parsed

    second = harness.ingest(_pdf(_resume_text("six years")), "second.pdf")
    await harness.pipeline(ContactParser(years=6)).run(second.revision_id)

    with harness.factory() as session:
        revision = session.get(ResumeRevision, second.revision_id)
        assert revision.manual_overrides == {"highest_degree": "博士"}
        # 人工改成的「博士」没有被新版解析（硕士）覆盖。
        assert revision.parsed_data["highest_degree"] == normalize_degree("博士")
        # 未被人工覆盖的字段仍以新版为准。
        assert revision.parsed_data["total_years"] == "6.0"


@pytest.mark.asyncio
async def test_failed_parse_keeps_the_previous_revision(harness: _Harness) -> None:
    """决策 D6：解析成功后才删旧版。解析失败时旧简历必须完好，否则会丢数据。"""
    first = harness.ingest(_pdf(_resume_text("five years")), "first.pdf")
    await harness.pipeline(ContactParser(years=5)).run(first.revision_id)
    second = harness.ingest(_pdf(_resume_text("six years")), "second.pdf")

    with pytest.raises(RuntimeError):
        await harness.pipeline(ExplodingParser()).run(second.revision_id)

    with harness.factory() as session:
        kept = session.get(ResumeRevision, first.revision_id)
        assert kept is not None and kept.is_current is True
        assert session.get(Candidate, first.candidate_id) is not None
        # 解析失败不做身份处理：本次导入的占位候选人仍独立存在，等重试。
        assert session.get(Candidate, second.candidate_id) is not None


@pytest.mark.asyncio
async def test_purge_repoints_history_and_cancels_pending_tasks(harness: _Harness) -> None:
    """历史匹配/复核结果改指新版本；指向被删版本的未终态任务必须取消。"""
    first = harness.ingest(_pdf(_resume_text("five years")), "first.pdf")
    await harness.pipeline(ContactParser(years=5)).run(first.revision_id)
    with harness.factory() as session, session.begin():
        run = MatchRun(trigger="reverse")
        session.add(run)
        session.flush()
        session.add(MatchResult(
            run_id=run.id,
            candidate_id=first.candidate_id,
            resume_revision_id=first.revision_id,
            total_score=1,
        ))
        session.add(SearchReview(
            query_key="q1", candidate_id=first.candidate_id, revision_id=first.revision_id,
        ))
        stale = TaskRecord(
            task_type="SEARCH_REVIEW",
            queue_name="batch",
            payload={"revision_id": first.revision_id},
            idempotency_key="stale-revision-task",
        )
        session.add(stale)
        session.flush()
        stale_id = stale.id

    second = harness.ingest(_pdf(_resume_text("six years")), "second.pdf")
    await harness.pipeline(ContactParser(years=6)).run(second.revision_id)

    with harness.factory() as session:
        result = session.scalar(select(MatchResult))
        assert result.resume_revision_id == second.revision_id
        review = session.scalar(select(SearchReview))
        assert review.revision_id == second.revision_id
        assert session.get(TaskRecord, stale_id).status == "CANCELLED"
