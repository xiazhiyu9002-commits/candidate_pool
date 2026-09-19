"""候选检索黄金集指标基线。

Task 1 目标：在改动检索实现之前，用一组固定的合成相关性样本，
把当前（ngram FTS）检索的 Precision@20 / Recall@20 / nDCG@10 记录为基线常量。
这些常量在 Task 10 作为非回归门禁使用，不在此处设置任何阈值。
"""
from __future__ import annotations

import asyncio
import json
import math
from pathlib import Path

import pytest

from kerui_recruit.providers.fakes import FakeRerankerProvider
from kerui_recruit.search.contracts import CandidateFilters, SearchChunk
from kerui_recruit.search.lancedb_index import LanceDBSearchIndex
from kerui_recruit.search.lexicon import concepts_from_query
from kerui_recruit.search.service import HybridSearchService


FIXTURE_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "search_golden.json"

# 基线常量：记录当前（ngram FTS）检索指标，Task 10 作为非回归门禁。
BASELINE_PRECISION_20: float = 0.03333333333333333  # 1/30
BASELINE_RECALL_20: float = 0.6666666666666666  # 2/3
BASELINE_NDCG_10: float = 0.6051549589285762


def load_golden_cases() -> tuple[list[dict], list[dict]]:
    """返回 (documents, queries)。"""
    with FIXTURE_PATH.open(encoding="utf-8") as handle:
        data = json.load(handle)
    return data["documents"], data["queries"]


def precision_at_k(retrieved: list[str], relevant: set[str], k: int) -> float:
    """二进制 Precision@k：top-k 中相关项占比。"""
    if k <= 0:
        return 0.0
    top = retrieved[:k]
    return sum(1 for item in top if item in relevant) / k


def recall_at_k(retrieved: list[str], relevant: set[str], k: int) -> float:
    """二进制 Recall@k：相关项中被 top-k 命中的比例。"""
    if not relevant:
        return 0.0
    top = set(retrieved[:k])
    return len(top & relevant) / len(relevant)


def ndcg_at_k(retrieved: list[str], relevance: dict[str, int], k: int) -> float:
    """分级 nDCG@k，相关性为任意非负整数。"""
    if k <= 0:
        return 0.0

    def dcg(items: list[str]) -> float:
        return sum(
            relevance.get(item, 0) / math.log2(idx + 2)
            for idx, item in enumerate(items[:k])
        )

    ideal = sorted(relevance.values(), reverse=True)[:k]
    idcg = sum(rel / math.log2(idx + 2) for idx, rel in enumerate(ideal))
    if idcg == 0.0:
        return 0.0
    return dcg(retrieved) / idcg


def _chunk(doc_id: str, text: str) -> SearchChunk:
    return SearchChunk(
        id=doc_id,
        candidate_id=doc_id,
        revision_id=f"r-{doc_id}",
        content=text,
        keyword_index_text=" ".join(text.split()).casefold(),
        vector=(1.0, 0.0),
        total_years=5,
        highest_degree="MASTER",
        location="上海",
        candidate_status="AVAILABLE",
    )


def _seed(index: LanceDBSearchIndex, documents: list[dict]) -> None:
    index.upsert([_chunk(doc["id"], doc["text"]) for doc in documents])


def _evaluate(index: LanceDBSearchIndex, queries: list[dict]) -> tuple[float, float, float, list[dict]]:
    per_query: list[dict] = []
    precisions: list[float] = []
    recalls: list[float] = []
    ndcgs: list[float] = []
    for query in queries:
        rows = index.search_fts(query["query"], CandidateFilters(), limit=20)
        retrieved = [row["candidate_id"] for row in rows]
        relevant = set(query["relevance"].keys())
        p = precision_at_k(retrieved, relevant, 20)
        r = recall_at_k(retrieved, relevant, 20)
        n = ndcg_at_k(retrieved, query["relevance"], 10)
        precisions.append(p)
        recalls.append(r)
        ndcgs.append(n)
        per_query.append({"query": query["query"], "retrieved": retrieved, "relevant": sorted(relevant)})
    count = len(queries)
    return (
        sum(precisions) / count,
        sum(recalls) / count,
        sum(ndcgs) / count,
        per_query,
    )


def test_golden_baseline_metrics_are_recorded(tmp_path) -> None:
    documents, queries = load_golden_cases()
    index = LanceDBSearchIndex(tmp_path, vector_dimension=2)
    _seed(index, documents)

    precision, recall, ndcg, per_query = _evaluate(index, queries)

    print("\n=== 候选检索黄金集基线 ===")
    for item in per_query:
        print(f"query={item['query']!r} relevant={item['relevant']} retrieved={item['retrieved']}")
    print(f"BASELINE_PRECISION_20={precision:.6f}")
    print(f"BASELINE_RECALL_20={recall:.6f}")
    print(f"BASELINE_NDCG_10={ndcg:.6f}")

    assert 0.0 <= precision <= 1.0
    assert 0.0 <= recall <= 1.0
    assert 0.0 <= ndcg <= 1.0


class _GoldenEmbedding:
    async def embed_query(self, text: str) -> list[float]:
        return [1.0, 0.0]

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.0] for _ in texts]


def _keyword_precision(index: LanceDBSearchIndex, queries: list[dict]) -> float:
    """关键词模式（BM25 词级 FTS）平均 Precision@20。"""
    values = []
    for query in queries:
        rows = index.search_fts(query["query"], CandidateFilters(), limit=20)
        retrieved = [row["candidate_id"] for row in rows]
        values.append(precision_at_k(retrieved, set(query["relevance"]), 20))
    return sum(values) / len(values)


def _alias_recall(index: LanceDBSearchIndex, queries: list[dict]) -> float:
    """别名展开（概念 OR 布尔召回）平均 Recall@20。"""
    values = []
    for query in queries:
        concepts = concepts_from_query(query["query"])
        rows = index.search_fts_boolean(query["query"], concepts, "or", CandidateFilters(), 20)
        retrieved = [row["candidate_id"] for row in rows]
        values.append(recall_at_k(retrieved, set(query["relevance"]), 20))
    return sum(values) / len(values)


async def _hybrid_ndcg(tmp_path, documents: list[dict], queries: list[dict]) -> float:
    """混合模式（FTS+向量融合+重排）平均 nDCG@10。"""
    index = LanceDBSearchIndex(tmp_path / "hybrid", vector_dimension=2)
    _seed(index, documents)
    service = HybridSearchService(
        index=index,
        embedding_provider=_GoldenEmbedding(),
        reranker_provider=FakeRerankerProvider(),
    )
    values = []
    for query in queries:
        page = await service.search(query["query"], CandidateFilters(), limit=20, mode="hybrid")
        retrieved = [hit.candidate_id for hit in page.items]
        values.append(ndcg_at_k(retrieved, query["relevance"], 10))
    return sum(values) / len(values)


def test_keyword_precision_meets_baseline(tmp_path) -> None:
    documents, queries = load_golden_cases()
    index = LanceDBSearchIndex(tmp_path, vector_dimension=2)
    _seed(index, documents)
    assert _keyword_precision(index, queries) >= BASELINE_PRECISION_20


def test_alias_recall_exceeds_baseline(tmp_path) -> None:
    documents, queries = load_golden_cases()
    index = LanceDBSearchIndex(tmp_path, vector_dimension=2)
    _seed(index, documents)
    assert _alias_recall(index, queries) > BASELINE_RECALL_20


@pytest.mark.asyncio
async def test_hybrid_ndcg_meets_baseline(tmp_path) -> None:
    documents, queries = load_golden_cases()
    ndcg = await _hybrid_ndcg(tmp_path, documents, queries)
    assert ndcg >= BASELINE_NDCG_10


def test_mandatory_alias_and_boundary_cases(tmp_path) -> None:
    documents, _ = load_golden_cases()
    index = LanceDBSearchIndex(tmp_path, vector_dimension=2)
    _seed(index, documents)

    # Java 不得因子串误匹配 JavaScript-only 候选人。
    java_hits = [r["candidate_id"] for r in index.search_fts("Java", CandidateFilters(), limit=20)]
    assert java_hits == ["java-backend"]

    # JS 必须命中 javascript-frontend（经别名）。
    js_hits = [r["candidate_id"] for r in index.search_fts_boolean("JS", concepts_from_query("JS"), "or", CandidateFilters(), 20)]
    assert "javascript-frontend" in js_hits

    # k8s 必须命中 kubernetes-platform（经别名）。
    k8s_hits = [r["candidate_id"] for r in index.search_fts_boolean("k8s", concepts_from_query("k8s"), "or", CandidateFilters(), 20)]
    assert "kubernetes-platform" in k8s_hits

    # C++ / C# / .NET / Node.js 词边界不被破坏。
    assert [r["candidate_id"] for r in index.search_fts("C++", CandidateFilters(), limit=20)] == ["cpp-engine"]
    assert "csharp-dotnet" in [r["candidate_id"] for r in index.search_fts("C# .NET", CandidateFilters(), limit=20)]
    assert "node-backend" in [r["candidate_id"] for r in index.search_fts("Node.js 后端", CandidateFilters(), limit=20)]
