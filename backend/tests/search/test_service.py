import asyncio
import time
from pathlib import Path

import pytest

from kerui_recruit.providers.errors import ProviderError
from kerui_recruit.providers.fakes import FakeRerankerProvider
from kerui_recruit.search import service as service_module
from kerui_recruit.search.contracts import CandidateFilters, SearchChunk, SearchHit
from kerui_recruit.search.lancedb_index import LanceDBSearchIndex
from kerui_recruit.search.lexicon import concepts_from_query
from kerui_recruit.search.rewrite import RewriteResult
from kerui_recruit.search.service import HybridSearchService, _pool_limit


class FixedQueryEmbedding:
    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.0] for _ in texts]

    async def embed_query(self, text: str) -> list[float]:
        assert text
        return [1.0, 0.0]


class FailingReranker:
    async def rerank(self, query: str, documents: list[str]) -> list[int]:
        raise ProviderError("E_API_BUSY", True, "busy")


def seed_index(tmp_path: Path) -> LanceDBSearchIndex:
    index = LanceDBSearchIndex(tmp_path / "search", vector_dimension=2)
    index.upsert(
        [
            SearchChunk("a1", "a", "r1", "Java payment", (1.0, 0.0), 5, "MASTER", "上海", "AVAILABLE"),
            SearchChunk("b1", "b", "r2", "finance platform", (0.9, 0.1), 5, "MASTER", "上海", "AVAILABLE"),
        ]
    )
    return index


@pytest.mark.asyncio
async def test_service_embeds_query_and_reranks_retrieved_candidates(tmp_path: Path) -> None:
    """Skipping query embedding or reranking must change this evidence-based order."""
    service = HybridSearchService(
        index=seed_index(tmp_path),
        embedding_provider=FixedQueryEmbedding(),
        reranker_provider=FakeRerankerProvider(),
    )

    page = await service.search("finance Java", CandidateFilters(), limit=20)

    assert [item.candidate_id for item in page.items] == ["a", "b"]
    assert page.degraded_reasons == ()


@pytest.mark.asyncio
async def test_service_returns_rrf_results_when_reranker_is_unavailable(tmp_path: Path) -> None:
    """A reranker outage must degrade search instead of making talent data unavailable."""
    service = HybridSearchService(
        index=seed_index(tmp_path),
        embedding_provider=FixedQueryEmbedding(),
        reranker_provider=FailingReranker(),
    )

    page = await service.search("Java finance", CandidateFilters(), limit=20)

    assert [item.candidate_id for item in page.items] == ["a", "b"]
    assert page.degraded_reasons == ("RERANKER_UNAVAILABLE",)


@pytest.mark.asyncio
async def test_keyword_and_vector_modes_return_search_hits(tmp_path: Path) -> None:
    """Single-channel modes must return SearchHit, never raw LanceDB dict rows."""
    service = HybridSearchService(
        index=seed_index(tmp_path),
        embedding_provider=FixedQueryEmbedding(),
        reranker_provider=FakeRerankerProvider(),
    )

    for mode in ("keyword", "vector"):
        page = await service.search("Java finance", CandidateFilters(), limit=5, mode=mode)
        assert page.items
        assert all(isinstance(item, SearchHit) for item in page.items)
        assert all(item.matched_channels for item in page.items)
        assert {item.candidate_id for item in page.items} == {"a", "b"}


class ScoredReranker:
    """返回明确的非排名相关性分数（0.07 / 0.02），用于断言精确传递。"""

    async def rerank_scored(self, query: str, documents: list[str]) -> list[tuple[int, float]]:
        scores = [0.07, 0.02]
        return [(index, scores[index]) for index in range(min(len(documents), len(scores)))]


def seed_multi_chunk_index(tmp_path: Path) -> LanceDBSearchIndex:
    index = LanceDBSearchIndex(tmp_path / "search", vector_dimension=2)
    index.upsert(
        [
            SearchChunk("a1", "a", "r1", "Java 支付", (1.0, 0.0), 5, "MASTER", "上海", "AVAILABLE"),
            SearchChunk("a2", "a", "r1", "Python 风控", (0.9, 0.1), 5, "MASTER", "上海", "AVAILABLE"),
            SearchChunk("b1", "b", "r2", "Go 后端", (0.0, 1.0), 3, "BACHELOR", "北京", "AVAILABLE"),
        ]
    )
    return index


@pytest.mark.asyncio
async def test_pure_filter_dedupes_by_candidate(tmp_path: Path) -> None:
    service = HybridSearchService(
        index=seed_multi_chunk_index(tmp_path),
        embedding_provider=FixedQueryEmbedding(),
        reranker_provider=FakeRerankerProvider(),
    )

    page = await service.search("", CandidateFilters(highest_degree="MASTER"), limit=10)

    # 候选 a 有 2 个 chunk，去重后只应出现一次。
    assert [h.candidate_id for h in page.items] == ["a"]


@pytest.mark.asyncio
async def test_pure_filter_excludes_skill(tmp_path: Path) -> None:
    service = HybridSearchService(
        index=seed_multi_chunk_index(tmp_path),
        embedding_provider=FixedQueryEmbedding(),
        reranker_provider=FakeRerankerProvider(),
    )

    page = await service.search("", CandidateFilters(exclude_skills=("Java",)), limit=10)

    # 排除 Java 后，只剩 b（含 Java 的 a 被排除）。
    assert [h.candidate_id for h in page.items] == ["b"]


@pytest.mark.asyncio
async def test_rerank_scored_uses_model_score(tmp_path: Path) -> None:
    service = HybridSearchService(
        index=seed_index(tmp_path),
        embedding_provider=FixedQueryEmbedding(),
        reranker_provider=ScoredReranker(),
    )

    page = await service.search("finance Java", CandidateFilters(), limit=20)

    # 模型分数应是真实分（非排名伪装），且重排后 a 在前。
    assert [h.candidate_id for h in page.items] == ["a", "b"]
    assert page.items[0].rerank_score == 0.07
    assert page.items[1].rerank_score == 0.02


class SlowEmbedding:
    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.0] for _ in texts]

    async def embed_query(self, text: str) -> list[float]:
        await asyncio.sleep(10)
        return [1.0, 0.0]


@pytest.mark.asyncio
async def test_fts_returned_when_embedding_times_out(tmp_path: Path) -> None:
    service = HybridSearchService(
        index=seed_index(tmp_path),
        embedding_provider=SlowEmbedding(),
        reranker_provider=FakeRerankerProvider(),
        search_timeout=0.6,
    )

    page = await service.search("Java finance", CandidateFilters(), limit=20)

    # 全文已成功，Embedding 超时，仍返回全文结果（不返回空）。
    assert {h.candidate_id for h in page.items} == {"a", "b"}
    assert "EMBEDDING_UNAVAILABLE" in page.degraded_reasons


@pytest.mark.asyncio
async def test_preferred_location_filter_projects_and_filters(tmp_path: Path) -> None:
    index = LanceDBSearchIndex(tmp_path / "search", vector_dimension=2)
    index.upsert(
        [
            SearchChunk("a1", "a", "r1", "Java", (1.0, 0.0), 5, "MASTER", "上海", "AVAILABLE", preferred_location="深圳"),
            SearchChunk("b1", "b", "r2", "Go", (0.0, 1.0), 3, "BACHELOR", "北京", "AVAILABLE", preferred_location="广州"),
        ]
    )
    service = HybridSearchService(
        index=index,
        embedding_provider=FixedQueryEmbedding(),
        reranker_provider=FakeRerankerProvider(),
    )

    page = await service.search("", CandidateFilters(preferred_locations=("深圳",)), limit=10)

    assert [h.candidate_id for h in page.items] == ["a"]


class SlowReranker:
    async def rerank(self, query: str, documents: list[str]) -> list[int]:
        await asyncio.sleep(10)
        return list(range(len(documents)))


@pytest.mark.asyncio
async def test_slow_reranker_keeps_fused_result(tmp_path: Path) -> None:
    service = HybridSearchService(
        index=seed_index(tmp_path),
        embedding_provider=FixedQueryEmbedding(),
        reranker_provider=SlowReranker(),
        search_timeout=0.6,
    )

    page = await service.search("Java finance", CandidateFilters(), limit=20)

    # 重排超时，仍返回融合结果并标记降级。
    assert {h.candidate_id for h in page.items} == {"a", "b"}
    assert "RERANKER_UNAVAILABLE" in page.degraded_reasons


@pytest.mark.asyncio
async def test_concurrent_searches_do_not_corrupt_results(tmp_path: Path) -> None:
    service = HybridSearchService(
        index=seed_index(tmp_path),
        embedding_provider=FixedQueryEmbedding(),
        reranker_provider=FakeRerankerProvider(),
    )

    pages = await asyncio.gather(
        *[service.search("Java finance", CandidateFilters(), limit=20) for _ in range(5)]
    )

    for page in pages:
        assert {h.candidate_id for h in page.items} == {"a", "b"}


def seed_boolean_index(tmp_path: Path) -> LanceDBSearchIndex:
    index = LanceDBSearchIndex(tmp_path / "search", vector_dimension=2)
    index.upsert(
        [
            SearchChunk("jb1", "java-backend", "r-jb", "Java 后端", (1.0, 0.0), 5, "MASTER", "上海", "AVAILABLE", keyword_index_text="java 后端"),
            SearchChunk("jn1", "java-non-backend", "r-jn", "Java 前端", (1.0, 0.0), 5, "MASTER", "上海", "AVAILABLE", keyword_index_text="java 前端"),
            SearchChunk("pb1", "python-backend", "r-pb", "Python 后端", (1.0, 0.0), 5, "MASTER", "上海", "AVAILABLE", keyword_index_text="python 后端"),
        ]
    )
    return index


def seed_alias_index(tmp_path: Path) -> LanceDBSearchIndex:
    index = seed_boolean_index(tmp_path)
    index.upsert(
        [
            SearchChunk("jsb1", "javascript-backend", "r-jsb", "JavaScript 后端", (1.0, 0.0), 5, "MASTER", "上海", "AVAILABLE", keyword_index_text="javascript 后端"),
        ]
    )
    return index


def boolean_service(tmp_path: Path) -> HybridSearchService:
    return HybridSearchService(
        index=seed_boolean_index(tmp_path),
        embedding_provider=FixedQueryEmbedding(),
        reranker_provider=FakeRerankerProvider(),
    )


@pytest.mark.asyncio
async def test_keyword_and_requires_every_concept(tmp_path: Path) -> None:
    service = boolean_service(tmp_path)
    page = await service.search(
        "Java 后端", CandidateFilters(), limit=20, mode="keyword",
        operator="and", concepts=concepts_from_query("Java 后端"),
    )
    assert [hit.candidate_id for hit in page.items] == ["java-backend"]


@pytest.mark.asyncio
async def test_keyword_or_accepts_any_concept(tmp_path: Path) -> None:
    service = boolean_service(tmp_path)
    page = await service.search(
        "Java 后端", CandidateFilters(), limit=20, mode="keyword",
        operator="or", concepts=concepts_from_query("Java 后端"),
    )
    assert {hit.candidate_id for hit in page.items} == {
        "java-backend", "java-non-backend", "python-backend"
    }


@pytest.mark.asyncio
async def test_aliases_are_or_within_and_concepts(tmp_path: Path) -> None:
    service = HybridSearchService(
        index=seed_alias_index(tmp_path),
        embedding_provider=FixedQueryEmbedding(),
        reranker_provider=FakeRerankerProvider(),
    )
    page = await service.search(
        "JS 后端", CandidateFilters(), limit=20, mode="keyword",
        operator="and", concepts=concepts_from_query("JS 后端"),
    )
    assert "javascript-backend" in {hit.candidate_id for hit in page.items}


@pytest.mark.asyncio
async def test_and_recall_survives_or_only_recall_cap(tmp_path: Path) -> None:
    index = LanceDBSearchIndex(tmp_path / "search", vector_dimension=2)
    chunks = [
        SearchChunk(f"o{i}", f"or-{i}", f"r-or-{i}", "Java 前端", (1.0, 0.0), 5, "MASTER", "上海", "AVAILABLE", keyword_index_text="java 前端")
        for i in range(320)
    ]
    chunks.append(SearchChunk("and1", "and-target", "r-and", "Java 后端", (1.0, 0.0), 5, "MASTER", "上海", "AVAILABLE", keyword_index_text="java 后端"))
    index.upsert(chunks)
    service = HybridSearchService(
        index=index,
        embedding_provider=FixedQueryEmbedding(),
        reranker_provider=FakeRerankerProvider(),
    )
    page = await service.search(
        "Java 后端", CandidateFilters(), limit=20, mode="keyword",
        operator="and", concepts=concepts_from_query("Java 后端"),
    )
    assert "and-target" in {hit.candidate_id for hit in page.items}


class SpyEmbedding:
    def __init__(self):
        self.calls: list[str] = []

    async def embed_query(self, text: str) -> list[float]:
        self.calls.append(text)
        return [1.0, 0.0]


class SpyReranker:
    def __init__(self):
        self.calls: list[str] = []

    async def rerank(self, query: str, documents: list[str]) -> list[int]:
        self.calls.append(query)
        return list(range(len(documents)))


class SpyRewriter:
    def __init__(self, rewritten: str = "rewritten query", *, sleep: float = 0.0):
        self.rewritten = rewritten
        self.sleep = sleep
        self.calls: list[str] = []

    async def rewrite(self, keywords: str) -> RewriteResult:
        self.calls.append(keywords)
        if self.sleep:
            await asyncio.sleep(self.sleep)
        return RewriteResult(self.rewritten, "success")


def spy_fts_index(tmp_path: Path):
    index = seed_index(tmp_path)
    fts_calls: list[str] = []
    original = index.search_fts

    def spy_search_fts(query, filters, limit):
        fts_calls.append(query)
        return original(query, filters, limit)

    index.search_fts = spy_search_fts  # type: ignore[method-assign]
    return index, fts_calls


@pytest.mark.asyncio
async def test_keyword_mode_never_calls_rewriter_embedding_reranker(tmp_path: Path) -> None:
    embedding = SpyEmbedding()
    reranker = SpyReranker()
    rewriter = SpyRewriter()
    service = HybridSearchService(
        index=seed_index(tmp_path),
        embedding_provider=embedding,
        reranker_provider=reranker,
        rewriter=rewriter,
    )
    page = await service.search("Java", CandidateFilters(), limit=20, mode="keyword", rewrite_enabled=True)
    assert page.items
    assert rewriter.calls == []
    assert embedding.calls == []
    assert reranker.calls == []


@pytest.mark.asyncio
async def test_vector_rewrite_disabled_sends_original_keywords(tmp_path: Path) -> None:
    embedding = SpyEmbedding()
    reranker = SpyReranker()
    rewriter = SpyRewriter()
    service = HybridSearchService(
        index=seed_index(tmp_path), embedding_provider=embedding, reranker_provider=reranker, rewriter=rewriter,
    )
    page = await service.search("Java 后端", CandidateFilters(), limit=20, mode="vector", rewrite_enabled=False)
    assert rewriter.calls == []
    assert embedding.calls == ["Java 后端"]
    assert reranker.calls == ["Java 后端"]
    assert page.query_plan.rewrite_status == "disabled"


@pytest.mark.asyncio
async def test_vector_rewrite_enabled_sends_rewritten_text(tmp_path: Path) -> None:
    embedding = SpyEmbedding()
    reranker = SpyReranker()
    rewriter = SpyRewriter(rewritten="Java 服务端")
    service = HybridSearchService(
        index=seed_index(tmp_path), embedding_provider=embedding, reranker_provider=reranker, rewriter=rewriter,
    )
    page = await service.search("Java 后端", CandidateFilters(), limit=20, mode="vector", rewrite_enabled=True)
    assert rewriter.calls == ["Java 后端"]
    assert embedding.calls == ["Java 服务端"]
    assert reranker.calls == ["Java 服务端"]
    assert page.query_plan.rewrite_status == "success"
    assert page.query_plan.semantic_query == "Java 服务端"


@pytest.mark.asyncio
async def test_hybrid_rewrite_enabled_fts_lexical_embedding_rewritten(tmp_path: Path) -> None:
    index, fts_calls = spy_fts_index(tmp_path)
    embedding = SpyEmbedding()
    reranker = SpyReranker()
    rewriter = SpyRewriter(rewritten="Java 服务端")
    service = HybridSearchService(
        index=index, embedding_provider=embedding, reranker_provider=reranker, rewriter=rewriter,
    )
    page = await service.search("Java 后端", CandidateFilters(), limit=20, mode="hybrid", rewrite_enabled=True)
    assert fts_calls == ["Java 后端"]
    assert embedding.calls == ["Java 服务端"]
    assert reranker.calls == ["Java 服务端"]
    assert page.query_plan.rewrite_status == "success"


class SlowRewriter:
    def __init__(self):
        self.calls: list[str] = []

    async def rewrite(self, keywords: str) -> RewriteResult:
        self.calls.append(keywords)
        await asyncio.sleep(10)
        return RewriteResult("should not be used", "success")


@pytest.mark.asyncio
async def test_rewriter_timeout_falls_back_to_original(tmp_path: Path) -> None:
    embedding = SpyEmbedding()
    reranker = SpyReranker()
    rewriter = SlowRewriter()
    service = HybridSearchService(
        index=seed_index(tmp_path), embedding_provider=embedding, reranker_provider=reranker,
        rewriter=rewriter, search_timeout=0.5,
    )
    page = await service.search("Java 后端", CandidateFilters(), limit=20, mode="vector", rewrite_enabled=True)
    assert page.query_plan.rewrite_status == "unavailable"
    assert page.query_plan.semantic_query is None
    assert embedding.calls == ["Java 后端"]
    assert reranker.calls == ["Java 后端"]


@pytest.mark.asyncio
async def test_hybrid_slow_rewriter_does_not_block_fts(tmp_path: Path) -> None:
    index, fts_calls = spy_fts_index(tmp_path)
    embedding = SpyEmbedding()
    reranker = SpyReranker()
    rewriter = SlowRewriter()
    service = HybridSearchService(
        index=index, embedding_provider=embedding, reranker_provider=reranker,
        rewriter=rewriter, search_timeout=4.5,
    )
    page = await service.search("Java 后端", CandidateFilters(), limit=20, mode="hybrid", rewrite_enabled=True)
    assert page.items
    assert fts_calls == ["Java 后端"]
    assert page.query_plan.rewrite_status == "unavailable"


def seed_body_index(tmp_path: Path) -> LanceDBSearchIndex:
    index = LanceDBSearchIndex(tmp_path / "search", vector_dimension=2)
    index.upsert(
        [
            SearchChunk("p1", "a", "r1", "Java 后端", (1.0, 0.0), 5, "MASTER", "上海", "AVAILABLE",
                        keyword_text="Java 后端", keyword_index_text="java 后端",
                        body_index_text="", chunk_type="parent"),
            SearchChunk("c1", "a", "r1", "支付服务", (0.9, 0.1), 5, "MASTER", "上海", "AVAILABLE",
                        keyword_text="支付 服务", keyword_index_text="支付 服务",
                        chunk_type="child", parent_id="r1"),
        ]
    )
    return index


@pytest.mark.asyncio
async def test_search_body_off_excludes_child_chunks(tmp_path: Path) -> None:
    service = HybridSearchService(
        index=seed_body_index(tmp_path),
        embedding_provider=FixedQueryEmbedding(),
        reranker_provider=FakeRerankerProvider(),
    )
    # 「支付」只出现在子 chunk 的 keyword_index_text，search_body=false 不应命中正文。
    page = await service.search("支付", CandidateFilters(), limit=10, mode="keyword", search_body=False)
    assert page.items == ()


@pytest.mark.asyncio
async def test_search_body_on_includes_child_chunks(tmp_path: Path) -> None:
    service = HybridSearchService(
        index=seed_body_index(tmp_path),
        embedding_provider=FixedQueryEmbedding(),
        reranker_provider=FakeRerankerProvider(),
    )
    page = await service.search("支付", CandidateFilters(), limit=10, mode="keyword", search_body=True)
    assert [h.candidate_id for h in page.items] == ["a"]


def seed_boolean_body_index(tmp_path: Path) -> LanceDBSearchIndex:
    """候选人 a 的两项技能被拆到 parent（java）与 child（redis）两个 chunk。"""
    index = LanceDBSearchIndex(tmp_path / "search", vector_dimension=2)
    index.upsert(
        [
            SearchChunk("p1", "a", "r1", "Java", (1.0, 0.0), 5, "MASTER", "上海", "AVAILABLE",
                        keyword_text="Java", keyword_index_text="java",
                        body_index_text="", chunk_type="parent"),
            SearchChunk("c1", "a", "r1", "Redis", (0.9, 0.1), 5, "MASTER", "上海", "AVAILABLE",
                        keyword_text="Redis", keyword_index_text="redis",
                        chunk_type="child", parent_id="r1"),
        ]
    )
    return index


@pytest.mark.asyncio
@pytest.mark.parametrize(("search_body", "expected"), [(False, ()), (True, ("a",))])
async def test_or_operator_respects_search_body(tmp_path: Path, search_body: bool,
                                                expected: tuple[str, ...]) -> None:
    """or 分支的「检索正文」开关必须真实生效：只出现在 child 的概念随开关可见。"""
    service = HybridSearchService(
        index=seed_boolean_body_index(tmp_path),
        embedding_provider=FixedQueryEmbedding(),
        reranker_provider=FakeRerankerProvider(),
    )
    page = await service.search(
        "Redis", CandidateFilters(), limit=10, mode="keyword",
        operator="or", concepts=concepts_from_query("Redis"), search_body=search_body,
    )
    assert tuple(h.candidate_id for h in page.items) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(("search_body", "expected"), [(False, ()), (True, ("a",))])
async def test_and_operator_is_candidate_level_and_respects_search_body(
    tmp_path: Path, search_body: bool, expected: tuple[str, ...]
) -> None:
    """and 在候选人级跨 chunk 求与：没有任何单 chunk 同时含 java+redis，只有聚合后 a 才完整。"""
    service = HybridSearchService(
        index=seed_boolean_body_index(tmp_path),
        embedding_provider=FixedQueryEmbedding(),
        reranker_provider=FakeRerankerProvider(),
    )
    page = await service.search(
        "Java Redis", CandidateFilters(), limit=10, mode="keyword",
        operator="and", concepts=concepts_from_query("Java Redis"), search_body=search_body,
    )
    assert tuple(h.candidate_id for h in page.items) == expected


def seed_boolean_generic_index(tmp_path: Path) -> LanceDBSearchIndex:
    """a 只有 java；b 有 java + 通用词「负责」；c 只有「负责」。"""
    index = LanceDBSearchIndex(tmp_path / "search", vector_dimension=2)
    index.upsert(
        [
            SearchChunk("a1", "a", "r1", "Java", (1.0, 0.0), 5, "MASTER", "上海", "AVAILABLE",
                        keyword_text="Java", keyword_index_text="java"),
            SearchChunk("b1", "b", "r2", "Java 负责", (0.9, 0.1), 5, "MASTER", "上海", "AVAILABLE",
                        keyword_text="Java 负责", keyword_index_text="java 负责"),
            SearchChunk("c1", "c", "r3", "负责", (0.8, 0.2), 5, "MASTER", "上海", "AVAILABLE",
                        keyword_text="负责", keyword_index_text="负责"),
        ]
    )
    return index


@pytest.mark.asyncio
async def test_and_operator_ignores_non_curated_generic_concepts(tmp_path: Path) -> None:
    """and 只对已策展技能概念求与：缺「负责」的 a 仍被召回，否则 JD 级查询结构性恒空。"""
    service = HybridSearchService(
        index=seed_boolean_generic_index(tmp_path),
        embedding_provider=FixedQueryEmbedding(),
        reranker_provider=FakeRerankerProvider(),
    )
    page = await service.search(
        "Java 负责", CandidateFilters(), limit=10, mode="keyword",
        operator="and", concepts=concepts_from_query("Java 负责"), search_body=False,
    )
    assert {h.candidate_id for h in page.items} == {"a", "b"}


@pytest.mark.asyncio
async def test_limit_controls_page_size(tmp_path: Path) -> None:
    index = LanceDBSearchIndex(tmp_path / "search", vector_dimension=2)
    chunks = [
        SearchChunk(f"c{i}", f"cand-{i}", f"r{i}", f"Java {i}", (1.0, 0.0), 5, "MASTER", "上海", "AVAILABLE")
        for i in range(50)
    ]
    index.upsert(chunks)
    service = HybridSearchService(
        index=index,
        embedding_provider=FixedQueryEmbedding(),
        reranker_provider=FakeRerankerProvider(),
    )
    page = await service.search("Java", CandidateFilters(), limit=7, mode="keyword")
    assert len(page.items) <= 7


@pytest.mark.asyncio
async def test_hybrid_respects_limit_beyond_30(tmp_path: Path) -> None:
    index = LanceDBSearchIndex(tmp_path / "search", vector_dimension=2)
    chunks = [
        SearchChunk(f"c{i}", f"cand-{i}", f"r{i}", f"Java 后端 {i}", (1.0, 0.0), 5, "MASTER", "上海", "AVAILABLE")
        for i in range(80)
    ]
    index.upsert(chunks)
    service = HybridSearchService(
        index=index,
        embedding_provider=FixedQueryEmbedding(),
        reranker_provider=FakeRerankerProvider(),
    )
    page = await service.search("Java 后端", CandidateFilters(), limit=80, mode="hybrid")
    assert len(page.items) == 80


def test_pool_limit_is_capped_at_120() -> None:
    assert _pool_limit(20) == 100
    assert _pool_limit(70) == 120
    assert _pool_limit(100) == 120
    assert _pool_limit(300) == 900


def provider_budget_service(tmp_path: Path) -> HybridSearchService:
    return HybridSearchService(
        index=seed_index(tmp_path),
        embedding_provider=FixedQueryEmbedding(),
        reranker_provider=FakeRerankerProvider(),
    )


@pytest.mark.asyncio
async def test_provider_slots_bound_concurrency(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(service_module, "_PROVIDER_MIN_INTERVAL", 0.001)
    service = provider_budget_service(tmp_path)
    active = 0
    peak = 0

    async def slow_call() -> str:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.05)
        active -= 1
        return "ok"

    deadline = time.monotonic() + 5.0
    results = await asyncio.gather(
        *(service._provider(slow_call, deadline=deadline) for _ in range(8))
    )

    # 超限排队而不是拒答：8 次调用全部完成，并发峰值不超过上限。
    assert results == ["ok"] * 8
    assert peak <= 4


@pytest.mark.asyncio
async def test_provider_pacing_throttles_sustained_rate(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(service_module, "_PROVIDER_MIN_INTERVAL", 0.05)
    service = provider_budget_service(tmp_path)

    async def quick_call() -> str:
        return "ok"

    deadline = time.monotonic() + 5.0
    started = time.monotonic()
    for _ in range(8):
        await service._provider(quick_call, deadline=deadline)
    elapsed = time.monotonic() - started

    # 突发额度 4 免费，其余 4 次按最小间隔放行（宽松下界，避免抖动误报）。
    assert elapsed >= 0.1

