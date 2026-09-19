"""Opt-in, local-only actual PDF verification; never uses model APIs or real DBs.

KERUI_REVIEW_PDF points to the user's original file. KERUI_REVIEW_OCR_JSON can
point to locally produced OCR [{"page_index": 0, "text": "..."}, ...].
The controlled mode proves orchestration, not OCR/model recognition accuracy.
"""
import json
import os
from pathlib import Path

import httpx
import pytest

from kerui_recruit.main import create_app
from kerui_recruit.resumes.extract import extract_text
from kerui_recruit.resumes.ingest import IngestResume, ResumeIngestService
from kerui_recruit.resumes.pipeline import PipelineFailure
from kerui_recruit.resumes.quality import analyze_text
from tests.api.test_local_api import build_services
from tests.resumes.test_pipeline_ocr import EmptyResumeParser, RecordingOCRProvider


@pytest.mark.asyncio
@pytest.mark.parametrize("ocr_mode", ["controlled", "offline_winrt"])
async def test_actual_pdf_routes_both_pages_and_review_is_read_only(tmp_path: Path, ocr_mode: str) -> None:
    path_value = os.environ.get("KERUI_REVIEW_PDF")
    if not path_value:
        pytest.skip("set KERUI_REVIEW_PDF for private local PDF acceptance")
    source = Path(path_value)
    original_bytes = source.read_bytes()
    extracted = extract_text(source)
    assert extracted.page_count == 2
    assert [page.page_index for page in extracted.pages if page.needs_ocr] == [0, 1]
    assert all(page.dominant_ratio > .95 for page in extracted.pages)
    pages = {
        0: "受控OCR测试正文：教育经历本科，负责支付风控平台运营与交易监控、合规政策实施。",
        1: "受控OCR测试第二页：工作经历涉及反洗钱规则、风险策略、客户尽职调查与项目管理。",
    }
    if ocr_mode == "offline_winrt":
        ocr_path = os.environ.get("KERUI_REVIEW_OCR_JSON")
        if not ocr_path:
            pytest.skip("set KERUI_REVIEW_OCR_JSON to verify actual offline recognition")
        pages = {item["page_index"]: item["text"] for item in
                 json.loads(Path(ocr_path).read_text(encoding="utf-8-sig"))}
        assert set(pages) == {0, 1}
        assert all(analyze_text(text).meaningful for text in pages.values())
        assert "反洗钱" in "".join(pages[1].split())
    services, pipeline = build_services(tmp_path)
    pipeline.parser = EmptyResumeParser()  # intentionally insufficient machine draft
    with services.session_factory() as session:
        result = ResumeIngestService(session, services.blob_store).ingest(
            IngestResume(filename="private-acceptance.pdf", content=original_bytes))
    with pytest.raises(PipelineFailure) as no_ocr:
        await pipeline.run(result.revision_id)
    assert no_ocr.value.code == "E_OCR_REQUIRED"
    ocr = RecordingOCRProvider(pages)
    pipeline.ocr_provider = ocr
    with pytest.raises(PipelineFailure) as sparse:
        await pipeline.run(result.revision_id)
    assert sparse.value.code == "E_PARSE_INCOMPLETE"
    assert ocr.called == [("extract_pages", [0, 1])]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(services)),
                               base_url="http://local", headers={"X-Kerui-Session": "test-token"}) as client:
        path = f"/api/resumes/revisions/{result.revision_id}/review"
        review = (await client.get(path)).json()
        assert review["status"] == "FAILED" and review["review_required"]
        assert review["raw_text"] == "\n".join(pages.values())
        assert [p["route"] for p in review["extraction_diagnostics"]["pages"]] == ["ocr", "ocr"]
    assert source.read_bytes() == original_bytes
