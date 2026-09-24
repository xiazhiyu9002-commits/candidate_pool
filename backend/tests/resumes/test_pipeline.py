import asyncio
import threading
from pathlib import Path

import pymupdf
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from kerui_recruit.db.migrate import migrate
from kerui_recruit.db.models import Candidate, CandidateContact, ResumeRevision
from kerui_recruit.db.session import create_engine_for
from kerui_recruit.encryption.service import EncryptionService
from kerui_recruit.providers.fakes import FakeEmbeddingProvider
from kerui_recruit.resumes.ingest import IngestResume, ResumeIngestService
from kerui_recruit.resumes.extract import UNKNOWN_NAME, ExtractedText
from kerui_recruit.resumes.pipeline import ResumePipeline
from kerui_recruit.resumes.structured import ParsedExperience, ParsedResume
from kerui_recruit.search.contracts import CandidateFilters, SearchRequest
from kerui_recruit.search.lancedb_index import LanceDBSearchIndex
from kerui_recruit.storage.blobs import BlobStore


class FixedResumeParser:
    async def parse_resume(self, text: str) -> ParsedResume:
        assert "Python Finance" in text
        return ParsedResume(
            name="张三",
            total_years=5,
            highest_degree="硕士",
            skills=["Python", "金融风控"],
            summary="金融科技后端工程师",
            experiences=[
                ParsedExperience(
                    company="示例科技",
                    title="后端工程师",
                    summary="负责 Python 风控平台",
                )
            ],
        )


class NamelessResumeParser:
    """模拟模型没能给出姓名（name 为 null）的解析结果。"""

    async def parse_resume(self, text: str) -> ParsedResume:
        return ParsedResume(
            name=None,
            total_years=5,
            highest_degree="硕士",
            skills=["Python", "金融风控"],
            summary="金融科技后端工程师，负责风控平台建设",
            experiences=[
                ParsedExperience(
                    company="示例科技",
                    title="后端工程师",
                    summary="负责 Python 风控平台",
                )
            ],
        )


class FixedOCRProvider:
    async def extract(self, content: bytes, filename: str) -> str:
        assert content.startswith(b"%PDF")
        assert filename.endswith(".pdf")
        return "Python Finance Resume recovered by OCR"


def make_pdf_bytes() -> bytes:
    pdf = pymupdf.open()
    pdf.new_page().insert_text((72, 72), "Python Finance Resume with five years")
    content = pdf.tobytes()
    pdf.close()
    return content


def make_blank_pdf_bytes() -> bytes:
    pdf = pymupdf.open()
    pdf.new_page()
    content = pdf.tobytes()
    pdf.close()
    return content


def make_pdf_bytes_with_contact() -> bytes:
    pdf = pymupdf.open()
    pdf.new_page().insert_text(
        (72, 72),
        "Python Finance Resume with five years\nEmail: zhang@example.com\nPhone: 13800138000",
    )
    content = pdf.tobytes()
    pdf.close()
    return content


def make_pdf_bytes_with_name_field() -> bytes:
    pdf = pymupdf.open()
    # 默认字体画不出中文，用内置简体字体，保证正文里真的有「姓名：李四」。
    pdf.new_page().insert_text(
        (72, 72), "个人简历\n姓名：李四\nPython 风控平台开发", fontname="china-s"
    )
    content = pdf.tobytes()
    pdf.close()
    return content


@pytest.mark.asyncio
async def test_pipeline_persists_facts_and_builds_embedded_search_chunks(
    tmp_path: Path,
) -> None:
    """A parse task must atomically leave both usable facts and searchable content."""
    engine = create_engine_for(tmp_path / "db" / "recruit.sqlite3")
    migrate(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    store = BlobStore(tmp_path / "blobs", tmp_path / "temp")
    with factory() as session:
        ingested = ResumeIngestService(session, store).ingest(
            IngestResume(filename="张三.pdf", content=make_pdf_bytes(), display_name="待解析")
        )
    pipeline = ResumePipeline(
        session_factory=factory,
        blob_store=store,
        parser=FixedResumeParser(),
        embedding_provider=FakeEmbeddingProvider(dimension=16),
    )

    result = await pipeline.run(ingested.revision_id)

    assert result.status == "READY"
    assert len(result.chunks) == 1
    assert all(len(chunk.vector) == 16 for chunk in result.chunks)
    assert result.chunks[0].keyword_text and result.chunks[0].vector_text
    with Session(engine) as session:
        candidate = session.get(Candidate, ingested.candidate_id)
        revision = session.get(ResumeRevision, ingested.revision_id)
        assert candidate is not None
        assert candidate.display_name == "张三"
        assert str(candidate.total_years) == "5.0"
        assert candidate.highest_degree == "MASTER"
        assert revision is not None
        assert revision.status == "READY"
        assert revision.parsed_data["skills"] == ["Python", "金融风控"]


@pytest.mark.asyncio
async def test_pipeline_recovers_name_from_text_when_model_returns_none(
    tmp_path: Path,
) -> None:
    """模型判空时用原文的姓名栏兜底，而不是拿文件名冒充姓名。"""
    engine = create_engine_for(tmp_path / "db" / "recruit.sqlite3")
    migrate(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    store = BlobStore(tmp_path / "blobs", tmp_path / "temp")
    with factory() as session:
        ingested = ResumeIngestService(session, store).ingest(
            IngestResume(filename="后端简历-final.pdf", content=make_pdf_bytes_with_name_field())
        )
    pipeline = ResumePipeline(
        session_factory=factory,
        blob_store=store,
        parser=NamelessResumeParser(),
        embedding_provider=FakeEmbeddingProvider(dimension=16),
    )

    result = await pipeline.run(ingested.revision_id)

    assert result.status == "READY"
    with Session(engine) as session:
        candidate = session.get(Candidate, ingested.candidate_id)
        assert candidate is not None
        assert candidate.display_name == "李四"


@pytest.mark.asyncio
async def test_pipeline_uses_placeholder_instead_of_filename(tmp_path: Path) -> None:
    """正文没有姓名线索、文件名也不是姓名形态时用中性占位，绝不拿它冒充姓名。"""
    engine = create_engine_for(tmp_path / "db" / "recruit.sqlite3")
    migrate(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    store = BlobStore(tmp_path / "blobs", tmp_path / "temp")
    with factory() as session:
        ingested = ResumeIngestService(session, store).ingest(
            IngestResume(filename="后端简历-final.pdf", content=make_pdf_bytes())
        )
        # 入库先落占位名，解析完成后才可能被真实姓名覆盖。
        assert session.get(Candidate, ingested.candidate_id).display_name == UNKNOWN_NAME
    pipeline = ResumePipeline(
        session_factory=factory,
        blob_store=store,
        parser=NamelessResumeParser(),
        embedding_provider=FakeEmbeddingProvider(dimension=16),
    )

    result = await pipeline.run(ingested.revision_id)

    assert result.status == "READY"
    with Session(engine) as session:
        candidate = session.get(Candidate, ingested.candidate_id)
        assert candidate is not None
        assert candidate.display_name == UNKNOWN_NAME


@pytest.mark.asyncio
async def test_pipeline_falls_back_to_name_shaped_filename(tmp_path: Path) -> None:
    """正文里没有姓名线索，但文件名本身就是完整姓名时采用它。"""
    engine = create_engine_for(tmp_path / "db" / "recruit.sqlite3")
    migrate(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    store = BlobStore(tmp_path / "blobs", tmp_path / "temp")
    with factory() as session:
        ingested = ResumeIngestService(session, store).ingest(
            IngestResume(filename="张伟.pdf", content=make_pdf_bytes())
        )
    pipeline = ResumePipeline(
        session_factory=factory,
        blob_store=store,
        parser=NamelessResumeParser(),
        embedding_provider=FakeEmbeddingProvider(dimension=16),
    )

    result = await pipeline.run(ingested.revision_id)

    assert result.status == "READY"
    with Session(engine) as session:
        candidate = session.get(Candidate, ingested.candidate_id)
        assert candidate is not None
        assert candidate.display_name == "张伟"


@pytest.mark.asyncio
async def test_pipeline_uses_ocr_provider_for_a_scanned_pdf(tmp_path: Path) -> None:
    """A scan without a text layer must be OCRed instead of silently indexing emptiness."""
    engine = create_engine_for(tmp_path / "db" / "recruit.sqlite3")
    migrate(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    store = BlobStore(tmp_path / "blobs", tmp_path / "temp")
    with factory() as session:
        ingested = ResumeIngestService(session, store).ingest(
            IngestResume(filename="扫描简历.pdf", content=make_blank_pdf_bytes())
        )
    pipeline = ResumePipeline(
        session_factory=factory,
        blob_store=store,
        parser=FixedResumeParser(),
        embedding_provider=FakeEmbeddingProvider(dimension=16),
        ocr_provider=FixedOCRProvider(),
    )

    result = await pipeline.run(ingested.revision_id)

    assert result.status == "READY"
    with Session(engine) as session:
        revision = session.get(ResumeRevision, ingested.revision_id)
        assert revision is not None
        assert revision.raw_text == "Python Finance Resume recovered by OCR"


@pytest.mark.asyncio
async def test_pipeline_writes_search_projection_before_marking_ready(tmp_path: Path) -> None:
    """A READY revision must be immediately discoverable in the embedded index."""
    engine = create_engine_for(tmp_path / "db" / "recruit.sqlite3")
    migrate(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    store = BlobStore(tmp_path / "blobs", tmp_path / "temp")
    with factory() as session:
        ingested = ResumeIngestService(session, store).ingest(
            IngestResume(filename="张三.pdf", content=make_pdf_bytes())
        )
    index = LanceDBSearchIndex(tmp_path / "search", vector_dimension=16)
    pipeline = ResumePipeline(
        session_factory=factory,
        blob_store=store,
        parser=FixedResumeParser(),
        embedding_provider=FakeEmbeddingProvider(dimension=16),
        search_index=index,
    )

    result = await pipeline.run(ingested.revision_id)
    hits = index.search(
        SearchRequest(
            "Python",
            result.chunks[0].vector,
            CandidateFilters(),
            limit=20,
        )
    )

    assert hits[0].candidate_id == ingested.candidate_id


@pytest.mark.asyncio
async def test_pipeline_encrypts_contact_details(tmp_path: Path) -> None:
    """Contact details must be stored encrypted, never as plain text."""
    engine = create_engine_for(tmp_path / "db" / "recruit.sqlite3")
    migrate(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    store = BlobStore(tmp_path / "blobs", tmp_path / "temp")
    with factory() as session:
        ingested = ResumeIngestService(session, store).ingest(
            IngestResume(filename="张三.pdf", content=make_pdf_bytes_with_contact())
        )
    encryption = EncryptionService(key_path=str(tmp_path / "encryption.key"))
    pipeline = ResumePipeline(
        session_factory=factory,
        blob_store=store,
        parser=FixedResumeParser(),
        embedding_provider=FakeEmbeddingProvider(dimension=16),
        encryption_service=encryption,
    )

    result = await pipeline.run(ingested.revision_id)

    assert result.status == "READY"
    with Session(engine) as session:
        contact = session.scalar(
            select(CandidateContact).where(
                CandidateContact.candidate_id == ingested.candidate_id
            )
        )
        assert contact is not None
        assert contact.email_encrypted is not None
        assert contact.phone_encrypted is not None
        assert contact.email_encrypted != "zhang@example.com"
        assert contact.phone_encrypted != "13800138000"
        assert encryption.decrypt(contact.email_encrypted) == "zhang@example.com"
        assert encryption.decrypt(contact.phone_encrypted) == "13800138000"
        assert contact.email_confidence == pytest.approx(0.9)
        assert contact.phone_confidence == pytest.approx(0.9)


@pytest.mark.asyncio
async def test_pipeline_extraction_does_not_block_desktop_api_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_engine_for(tmp_path / "db" / "recruit.sqlite3")
    migrate(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    store = BlobStore(tmp_path / "blobs", tmp_path / "temp")
    with factory() as session:
        ingested = ResumeIngestService(session, store).ingest(
            IngestResume(filename="candidate.pdf", content=make_pdf_bytes())
        )

    release = threading.Event()

    def blocking_extract(_path: Path) -> ExtractedText:
        release.wait(timeout=2)
        return ExtractedText(
            text="Python Finance Resume with five years",
            page_count=1,
            requires_ocr=False,
        )

    monkeypatch.setattr("kerui_recruit.resumes.pipeline.extract_text", blocking_extract)
    timer = threading.Timer(0.75, release.set)
    timer.daemon = True
    timer.start()
    pipeline = ResumePipeline(
        session_factory=factory,
        blob_store=store,
        parser=FixedResumeParser(),
        embedding_provider=FakeEmbeddingProvider(dimension=16),
    )

    started = asyncio.get_running_loop().time()
    task = asyncio.create_task(pipeline.run(ingested.revision_id))
    await asyncio.sleep(0.05)
    elapsed = asyncio.get_running_loop().time() - started
    await task

    assert elapsed < 0.5


@pytest.mark.asyncio
async def test_cancelling_pipeline_does_not_leave_revision_processing(tmp_path: Path) -> None:
    engine = create_engine_for(tmp_path / "db" / "recruit.sqlite3")
    migrate(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    store = BlobStore(tmp_path / "blobs", tmp_path / "temp")
    with factory() as session:
        ingested = ResumeIngestService(session, store).ingest(
            IngestResume(filename="candidate.pdf", content=make_pdf_bytes())
        )

    started = asyncio.Event()

    class BlockingParser:
        async def parse_resume(self, _text: str) -> ParsedResume:
            started.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    pipeline = ResumePipeline(
        session_factory=factory,
        blob_store=store,
        parser=BlockingParser(),
        embedding_provider=FakeEmbeddingProvider(dimension=16),
    )
    task = asyncio.create_task(pipeline.run(ingested.revision_id))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    with factory() as session:
        revision = session.get(ResumeRevision, ingested.revision_id)
        assert revision.status == "FAILED"
        assert revision.error_code == "E_TASK_CANCELLED"


class LocalFallbackResumeParser:
    """模拟「没有可用快速解析路由」时的本地兜底解析器（`_RoutedResumeParser` 的形态）。"""

    def uses_remote_ai(self) -> bool:
        return False

    async def parse_resume(self, text: str) -> ParsedResume:
        return await FixedResumeParser().parse_resume(text)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("parser", "expected_route"),
    [(LocalFallbackResumeParser(), "local"), (FixedResumeParser(), "remote")],
)
async def test_pipeline_records_which_parse_route_was_used(tmp_path: Path, parser, expected_route: str) -> None:
    """实际走的解析路线必须写进抽取诊断。

    真机证据（2026-09-22 智谱/火山两轮验收）：连接缺少快速解析角色时，解析会静默回退本地
    确定性解析，任务 0.2~3.6 秒就「成功」，界面显示解析完成而画像为空，被误读成
    「该供应商解析质量差」。路线可读之后这种降级才解释得通。
    不认识 `uses_remote_ai` 的解析器按「远程」记录（失败开放，不凭空制造降级结论）。
    """
    engine = create_engine_for(tmp_path / "db" / "recruit.sqlite3")
    migrate(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    store = BlobStore(tmp_path / "blobs", tmp_path / "temp")
    with factory() as session:
        ingested = ResumeIngestService(session, store).ingest(
            IngestResume(filename="张三.pdf", content=make_pdf_bytes())
        )

    pipeline = ResumePipeline(
        session_factory=factory,
        blob_store=store,
        parser=parser,
        embedding_provider=FakeEmbeddingProvider(dimension=16),
    )
    await pipeline.run(ingested.revision_id)

    with Session(engine) as session:
        revision = session.get(ResumeRevision, ingested.revision_id)
        assert revision.extraction_diagnostics["ai_parse_route"] == expected_route


@pytest.mark.asyncio
async def test_pipeline_sets_profile_metadata_on_first_parse(tmp_path: Path) -> None:
    """初次解析产生非空画像时，必须补全 source=ai、input_hash、stale=false。"""

    class ProfileParser:
        async def parse_resume(self, text: str) -> ParsedResume:
            return ParsedResume(
                name="张三",
                total_years=5,
                highest_degree="硕士",
                skills=["Python"],
                summary="资深后端工程师，拥有多年服务端开发经验，熟悉 Python 与分布式系统架构设计。",
                ai_profile_summary="资深后端工程师",
            )

    engine = create_engine_for(tmp_path / "db" / "recruit.sqlite3")
    migrate(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    store = BlobStore(tmp_path / "blobs", tmp_path / "temp")
    with factory() as session:
        ingested = ResumeIngestService(session, store).ingest(
            IngestResume(filename="张三.pdf", content=make_pdf_bytes(), display_name="待解析")
        )
    pipeline = ResumePipeline(
        session_factory=factory,
        blob_store=store,
        parser=ProfileParser(),
        embedding_provider=FakeEmbeddingProvider(dimension=16),
    )
    result = await pipeline.run(ingested.revision_id)
    assert result.status == "READY"
    with Session(engine) as session:
        revision = session.get(ResumeRevision, ingested.revision_id)
        assert revision.parsed_data["ai_profile_summary"] == "资深后端工程师"
        assert revision.parsed_data["ai_profile_source"] == "ai"
        assert revision.parsed_data["ai_profile_input_hash"]
        assert revision.parsed_data["ai_profile_stale"] is False
