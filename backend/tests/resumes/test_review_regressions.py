from pathlib import Path

import pytest

from kerui_recruit.db.models import ResumeRevision
from kerui_recruit.providers.fakes import FakeEmbeddingProvider
from kerui_recruit.resumes.pipeline import PipelineFailure, ResumePipeline
from kerui_recruit.resumes.structured import ParsedResume
from kerui_recruit.resumes.validity import check_parsed_resume
from tests.resumes.test_pipeline_ocr import (
    EmptyResumeParser, FixedResumeParser, RecordingOCRProvider,
    _blank_pdf_bytes, _ingest, _mixed_pdf_bytes, _text_pdf_bytes,
)


@pytest.mark.asyncio
async def test_failed_extraction_is_visible_review_with_source_evidence(tmp_path: Path) -> None:
    store, factory, revision_id = _ingest(tmp_path, "resume.pdf", _text_pdf_bytes())
    pipeline = ResumePipeline(session_factory=factory, blob_store=store,
        parser=EmptyResumeParser(), embedding_provider=FakeEmbeddingProvider(dimension=16))
    with pytest.raises(PipelineFailure):
        await pipeline.run(revision_id)
    with factory() as session:
        revision = session.get(ResumeRevision, revision_id)
        assert revision.document.candidate.status == "AVAILABLE"
        assert "Python Finance" in revision.raw_text
        assert revision.review_data["name"] is None
        assert revision.extraction_diagnostics["pages"][0]["route"] == "direct"
        assert revision.parsed_data is None


@pytest.mark.asyncio
async def test_punctuation_ocr_never_reaches_structuring(tmp_path: Path) -> None:
    store, factory, revision_id = _ingest(tmp_path, "scan.pdf", _blank_pdf_bytes())
    pipeline = ResumePipeline(session_factory=factory, blob_store=store,
        parser=FixedResumeParser(), embedding_provider=FakeEmbeddingProvider(dimension=16),
        ocr_provider=RecordingOCRProvider({0: "???? ----- ...."}))
    with pytest.raises(PipelineFailure) as error:
        await pipeline.run(revision_id)
    assert error.value.code == "E_OCR_LOW_QUALITY"


@pytest.mark.asyncio
async def test_legacy_whole_ocr_receives_only_requested_single_pages(tmp_path: Path) -> None:
    import pymupdf
    class LegacyOCR:
        async def extract(self, content: bytes, filename: str) -> str:
            with pymupdf.open(stream=content, filetype="pdf") as document:
                assert document.page_count == 1
                assert "Python Finance" not in document[0].get_text()
            return "Legacy OCR second page resume details"
    store, factory, revision_id = _ingest(tmp_path, "mixed.pdf", _mixed_pdf_bytes())
    pipeline = ResumePipeline(session_factory=factory, blob_store=store,
        parser=FixedResumeParser(), embedding_provider=FakeEmbeddingProvider(dimension=16),
        ocr_provider=LegacyOCR())
    await pipeline.run(revision_id)
    with factory() as session:
        revision = session.get(ResumeRevision, revision_id)
        assert revision.raw_text.count("Python Finance") == 1


@pytest.mark.asyncio
async def test_reparse_does_not_release_on_hold_candidate(tmp_path: Path) -> None:
    store, factory, revision_id = _ingest(tmp_path, "resume.pdf", _text_pdf_bytes())
    pipeline = ResumePipeline(session_factory=factory, blob_store=store,
        parser=FixedResumeParser(), embedding_provider=FakeEmbeddingProvider(dimension=16))
    await pipeline.run(revision_id)
    with factory() as session, session.begin():
        session.get(ResumeRevision, revision_id).document.candidate.status = "ON_HOLD"
    await pipeline.run(revision_id)
    with factory() as session:
        assert session.get(ResumeRevision, revision_id).document.candidate.status == "ON_HOLD"


def test_demographics_only_and_blank_lists_require_review() -> None:
    source = "A readable resume with substantial work and educational details."
    for parsed in [ParsedResume(name="张三", location="广州"),
                   ParsedResume(name="张三", skills=["  "], highest_degree=" ")]:
        assert check_parsed_resume(parsed, source).ok is False


@pytest.mark.asyncio
async def test_manual_edit_during_embedding_is_not_overwritten(tmp_path: Path) -> None:
    store, factory, revision_id = _ingest(tmp_path, "resume.pdf", _text_pdf_bytes())
    pipeline = ResumePipeline(session_factory=factory, blob_store=store,
        parser=FixedResumeParser(), embedding_provider=FakeEmbeddingProvider(dimension=16))
    await pipeline.run(revision_id)
    class EditingEmbedding(FakeEmbeddingProvider):
        async def embed_documents(self, texts):
            with factory() as session, session.begin():
                revision = session.get(ResumeRevision, revision_id)
                revision.manual_overrides = {"name": "手动修改"}
                revision.parsed_data = {**revision.parsed_data, "name": "手动修改"}
            return await super().embed_documents(texts)
    pipeline.embedding_provider = EditingEmbedding(dimension=16)
    with pytest.raises(PipelineFailure) as error:
        await pipeline.run(revision_id)
    assert error.value.code == "E_REPARSE_CONFLICT"
    with factory() as session:
        revision = session.get(ResumeRevision, revision_id)
        assert revision.parsed_data["name"] == "手动修改"
        assert revision.status == "READY"


@pytest.mark.asyncio
async def test_preferred_cities_survive_parsing_and_projection(tmp_path: Path) -> None:
    class MultiCityParser:
        async def parse_resume(self, text):
            return ParsedResume(name="张三", skills=["Python"], location="上海",
                preferred_location="广州", preferred_locations=[" 深圳 ", "杭州", "深圳"],
                summary="多城市工作经历，熟悉 Python 后端开发与跨地域项目交付，具备多年工程经验。")
    store, factory, revision_id = _ingest(tmp_path, "resume.pdf", _text_pdf_bytes())
    result = await ResumePipeline(session_factory=factory, blob_store=store,
        parser=MultiCityParser(), embedding_provider=FakeEmbeddingProvider(dimension=16)).run(revision_id)
    assert result.chunks[0].location == "上海"
    assert result.chunks[0].preferred_location == "广州"
    assert result.chunks[0].preferred_locations == ("深圳", "杭州")
    with factory() as session:
        assert session.get(ResumeRevision, revision_id).parsed_data["preferred_locations"] == ["深圳", "杭州"]


@pytest.mark.asyncio
@pytest.mark.parametrize("manual_age", [44, None])
async def test_manual_derived_age_and_clear_survive_reparse(tmp_path: Path, manual_age) -> None:
    class AgeParser:
        async def parse_resume(self, text):
            return ParsedResume(name="张三", skills=["Python"], birth_year=1990,
                summary="资深后端工程师，拥有多年服务端开发经验，熟悉 Python 与分布式系统架构设计，参与过多个大型项目。")
    store, factory, revision_id = _ingest(tmp_path, "resume.pdf", _text_pdf_bytes())
    with factory() as session, session.begin():
        session.get(ResumeRevision, revision_id).manual_overrides = {"age": manual_age}
    await ResumePipeline(session_factory=factory, blob_store=store,
        parser=AgeParser(), embedding_provider=FakeEmbeddingProvider(dimension=16)).run(revision_id)
    with factory() as session:
        assert session.get(ResumeRevision, revision_id).parsed_data["age"] == manual_age
