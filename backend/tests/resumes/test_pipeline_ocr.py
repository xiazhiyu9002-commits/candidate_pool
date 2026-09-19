from __future__ import annotations

from pathlib import Path

import pymupdf
import pytest
from sqlalchemy.orm import sessionmaker

from kerui_recruit.db.migrate import migrate
from kerui_recruit.db.models import ResumeRevision
from kerui_recruit.db.session import create_engine_for
from kerui_recruit.providers.fakes import FakeEmbeddingProvider
from kerui_recruit.resumes.ingest import IngestResume, ResumeIngestService
from kerui_recruit.resumes.pipeline import PipelineFailure, ResumePipeline
from kerui_recruit.resumes.structured import ParsedExperience, ParsedResume
from kerui_recruit.storage.blobs import BlobStore


class FixedResumeParser:
    async def parse_resume(self, text: str) -> ParsedResume:
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


class EmptyResumeParser:
    async def parse_resume(self, text: str) -> ParsedResume:
        return ParsedResume()


class EmptyOCRProvider:
    async def extract(self, content: bytes, filename: str) -> str:
        return ""

    async def extract_pages(self, content: bytes, filename: str, page_indexes: list[int]) -> list[str]:
        return [""] * len(page_indexes)


class RecordingOCRProvider:
    def __init__(self, pages: dict[int, str]) -> None:
        self.pages = pages
        self.called: list[tuple] = []

    async def extract(self, content: bytes, filename: str) -> str:
        self.called.append(("extract", None))
        return "\n".join(self.pages.values())

    async def extract_pages(self, content: bytes, filename: str, page_indexes: list[int]) -> list[str]:
        self.called.append(("extract_pages", list(page_indexes)))
        return [self.pages.get(i, f"OCR-第{i+1}页") for i in page_indexes]


def _image_pixmap(width: int, height: int) -> pymupdf.Pixmap:
    pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, width, height))
    pixmap.clear_with(0)
    return pixmap


def _text_pdf_bytes() -> bytes:
    pdf = pymupdf.open()
    pdf.new_page().insert_text((72, 72), "Python Finance Resume with five years")
    content = pdf.tobytes()
    pdf.close()
    return content


def _blank_pdf_bytes() -> bytes:
    pdf = pymupdf.open()
    pdf.new_page()
    content = pdf.tobytes()
    pdf.close()
    return content


def _mixed_pdf_bytes() -> bytes:
    pdf = pymupdf.open()
    text_page = pdf.new_page()
    text_page.insert_text((72, 72), "Python Finance Resume with five years")
    image_page = pdf.new_page()
    image_page.insert_image(image_page.rect, pixmap=_image_pixmap(600, 800))
    content = pdf.tobytes()
    pdf.close()
    return content


def _ingest(tmp_path: Path, filename: str, content: bytes) -> tuple[BlobStore, sessionmaker, str]:
    engine = create_engine_for(tmp_path / "db" / "recruit.sqlite3")
    migrate(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    store = BlobStore(tmp_path / "blobs", tmp_path / "temp")
    with factory() as session:
        result = ResumeIngestService(session, store).ingest(
            IngestResume(filename=filename, content=content)
        )
    return store, factory, result.revision_id


@pytest.mark.asyncio
async def test_missing_ocr_provider_marks_ocr_required(tmp_path: Path) -> None:
    store, factory, revision_id = _ingest(tmp_path, "scan.pdf", _blank_pdf_bytes())
    pipeline = ResumePipeline(
        session_factory=factory,
        blob_store=store,
        parser=FixedResumeParser(),
        embedding_provider=FakeEmbeddingProvider(dimension=16),
    )

    with pytest.raises(PipelineFailure) as error:
        await pipeline.run(revision_id)

    assert error.value.code == "E_OCR_REQUIRED"
    with factory() as session:
        revision = session.get(ResumeRevision, revision_id)
        assert revision.status == "FAILED"
        assert revision.error_code == "E_OCR_REQUIRED"


@pytest.mark.asyncio
async def test_empty_ocr_result_is_not_marked_ready(tmp_path: Path) -> None:
    store, factory, revision_id = _ingest(tmp_path, "scan.pdf", _blank_pdf_bytes())
    pipeline = ResumePipeline(
        session_factory=factory,
        blob_store=store,
        parser=FixedResumeParser(),
        embedding_provider=FakeEmbeddingProvider(dimension=16),
        ocr_provider=EmptyOCRProvider(),
    )

    with pytest.raises(PipelineFailure) as error:
        await pipeline.run(revision_id)

    assert error.value.code == "E_OCR_EMPTY"


@pytest.mark.asyncio
async def test_empty_parsed_result_is_not_marked_ready(tmp_path: Path) -> None:
    store, factory, revision_id = _ingest(tmp_path, "张三.pdf", _text_pdf_bytes())
    pipeline = ResumePipeline(
        session_factory=factory,
        blob_store=store,
        parser=EmptyResumeParser(),
        embedding_provider=FakeEmbeddingProvider(dimension=16),
    )

    with pytest.raises(PipelineFailure) as error:
        await pipeline.run(revision_id)

    assert error.value.code == "E_PARSE_INCOMPLETE"
    with factory() as session:
        revision = session.get(ResumeRevision, revision_id)
        assert revision.status == "FAILED"
        assert revision.parsed_data is None


@pytest.mark.asyncio
async def test_force_ocr_bypasses_auto_routing(tmp_path: Path) -> None:
    store, factory, revision_id = _ingest(tmp_path, "张三.pdf", _text_pdf_bytes())
    ocr = RecordingOCRProvider({0: "OCR 识别出的简历正文"})
    pipeline = ResumePipeline(
        session_factory=factory,
        blob_store=store,
        parser=FixedResumeParser(),
        embedding_provider=FakeEmbeddingProvider(dimension=16),
        ocr_provider=ocr,
    )

    result = await pipeline.run(revision_id, force_ocr=True)

    assert result.status == "READY"
    assert ocr.called and ocr.called[0][0] == "extract_pages"


@pytest.mark.asyncio
async def test_reparse_failure_preserves_previous_ready(tmp_path: Path) -> None:
    store, factory, revision_id = _ingest(tmp_path, "张三.pdf", _text_pdf_bytes())
    pipeline = ResumePipeline(
        session_factory=factory,
        blob_store=store,
        parser=FixedResumeParser(),
        embedding_provider=FakeEmbeddingProvider(dimension=16),
    )
    await pipeline.run(revision_id)

    failing = ResumePipeline(
        session_factory=factory,
        blob_store=store,
        parser=FixedResumeParser(),
        embedding_provider=FakeEmbeddingProvider(dimension=16),
        ocr_provider=EmptyOCRProvider(),
    )
    with pytest.raises(PipelineFailure):
        await failing.run(revision_id, force_ocr=True)

    with factory() as session:
        revision = session.get(ResumeRevision, revision_id)
        assert revision.status == "READY"
        assert revision.parsed_data is not None
        assert revision.parsed_data["name"] == "张三"


@pytest.mark.asyncio
async def test_mixed_pdf_preserves_page_order(tmp_path: Path) -> None:
    store, factory, revision_id = _ingest(tmp_path, "mixed.pdf", _mixed_pdf_bytes())
    ocr = RecordingOCRProvider({1: "OCR 第二页正文"})
    pipeline = ResumePipeline(
        session_factory=factory,
        blob_store=store,
        parser=FixedResumeParser(),
        embedding_provider=FakeEmbeddingProvider(dimension=16),
        ocr_provider=ocr,
    )

    result = await pipeline.run(revision_id)

    assert result.status == "READY"
    with factory() as session:
        revision = session.get(ResumeRevision, revision_id)
        raw = revision.raw_text or ""
        assert raw.index("Python Finance Resume") < raw.index("OCR 第二页正文")
