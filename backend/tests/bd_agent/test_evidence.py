from __future__ import annotations

import pytest

from kerui_recruit.bd_agent.evidence import (
    EvidenceDoc,
    EvidenceExtractor,
    RankedChunk,
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
    # 中性站点压到 0.2：排序先看来源质量、再看重排顺序，而 top-10 是所有来源共用的名额，
    # 中性站点排在前面就会把官网(0.8)的片段挤出去。
    assert source_quality("https://example.com/page") == 0.2


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
async def test_extract_keeps_page_title_on_chunks() -> None:
    """标题必须随 chunk 一起流到综合环节。

    招聘站正文通常只有岗位职责，雇主名在页面标题里（「XX科技招聘算法工程师-北京-BOSS直聘」）。
    早先这里把 title 丢掉，公司名因此无从提取，线索大批落成「未识别公司」。
    """
    docs = [
        EvidenceDoc(
            source_url="https://www.zhipin.com/job/1",
            title="XX科技招聘算法工程师-北京",
            content="负责推荐系统召回。",
        )
    ]
    extractor = EvidenceExtractor(reranker=None, top_k=5)
    chunks = await extractor.extract("q", docs)
    assert chunks
    assert chunks[0].title == "XX科技招聘算法工程师-北京"


def test_ranked_chunk_title_is_optional_and_appended_last() -> None:
    """title 有默认值且排在 score 之后：既有按位置构造的调用不受影响。"""
    chunk = RankedChunk("正文", "https://a.com", 0.5)
    assert chunk.title is None
    assert chunk.score == 0.5


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
