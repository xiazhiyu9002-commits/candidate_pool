from __future__ import annotations

import pytest

from kerui_recruit.bd_agent.evidence import (
    EvidenceDoc,
    EvidenceExtractor,
    _chunk_text,
    source_quality,
)


def test_chunk_text_splits_long_content() -> None:
    text = "句子一。" * 200
    chunks = _chunk_text(text, max_len=50)
    assert len(chunks) > 1


def test_source_quality_classifies_sources() -> None:
    assert source_quality("https://www.zhipin.com/job_detail/1") == 1.0
    assert source_quality("https://www.liepin.com/job/1") == 1.0
    assert source_quality("https://cn.linkedin.com/jobs/view/1") == 1.0
    assert source_quality("https://www.zhihu.com/question/1") == 0.0
    assert source_quality("https://www.163.com/news/1") == 0.0
    assert source_quality("https://jobs.bytedance.com/careers/1") == 0.8
    assert source_quality("https://example.com/page") == 0.5


@pytest.mark.asyncio
async def test_extract_returns_top_k_without_reranker() -> None:
    docs = [
        EvidenceDoc(
            source_url="https://a.com",
            title="t",
            content="第一句。第二句。第三句。",
        )
    ]
    extractor = EvidenceExtractor(reranker=None, top_k=2)
    chunks = await extractor.extract("query", docs)
    assert len(chunks) <= 2
    assert all(chunk.source_url == "https://a.com" for chunk in chunks)


@pytest.mark.asyncio
async def test_extract_filters_low_quality_sources() -> None:
    docs = [
        EvidenceDoc(source_url="https://www.zhihu.com/q/1", title="t", content="知乎文章内容。"),
        EvidenceDoc(source_url="https://www.zhipin.com/job/1", title="t", content="BOSS直聘岗位。"),
    ]
    extractor = EvidenceExtractor(reranker=None, top_k=5)
    chunks = await extractor.extract("q", docs)
    assert chunks
    assert all(chunk.source_url == "https://www.zhipin.com/job/1" for chunk in chunks)


@pytest.mark.asyncio
async def test_extract_prefers_high_quality_sources() -> None:
    docs = [
        EvidenceDoc(source_url="https://example.com/page", title="t", content="中性来源岗位。"),
        EvidenceDoc(source_url="https://www.zhipin.com/job/1", title="t", content="BOSS直聘岗位。"),
    ]
    extractor = EvidenceExtractor(reranker=None, top_k=5)
    chunks = await extractor.extract("q", docs)
    assert chunks[0].source_url == "https://www.zhipin.com/job/1"


@pytest.mark.asyncio
async def test_extract_uses_reranker_order() -> None:
    captured: dict[str, list[str]] = {}

    class FakeReranker:
        async def rerank(self, query, documents):
            captured["docs"] = documents
            return list(range(len(documents)))[::-1]  # 反转顺序

    long_text = "句子内容。" * 300
    docs = [
        EvidenceDoc(source_url="https://a.com", title="t", content=long_text)
    ]
    extractor = EvidenceExtractor(reranker=FakeReranker(), top_k=3)  # type: ignore[arg-type]
    chunks = await extractor.extract("q", docs)
    assert len(captured["docs"]) >= 3
    assert len(chunks) == 3
