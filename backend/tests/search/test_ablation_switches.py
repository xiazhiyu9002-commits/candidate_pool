"""消融旋钮的有效性验证：旋钮必须真的改变召回或排序，否则「档位」是空壳。

阶段 1（混合正文检索）：
- ``HYBRID_WEAK_HIT_FILTER``：混合通道的 FTS 弱命中过滤开关，默认开启 = 现网行为。

阶段 4：
- ``VECTOR_KIND_RECALL_QUOTA``：开启后向量召回按 kind 拆分，rank 在每个 kind 列表内从 1 重新开始；
- ``FUSION_WEIGHT_BM25`` / ``FUSION_WEIGHT_VECTOR``：权重真的进入融合分计算；
- ``CONCEPT_COVERAGE_MODE``：off / tiebreak / light 三档只改排序，不改公开 score。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from kerui_recruit.search import lancedb_index as index_module
from kerui_recruit.search import service as service_module
from kerui_recruit.search.contracts import CandidateFilters, SearchChunk, SearchHit
from kerui_recruit.search.lancedb_index import (
    CHANNEL_BM25,
    CHANNEL_VECTOR_ORIGINAL,
    LanceDBSearchIndex,
)
from kerui_recruit.search.service import HybridSearchService, _apply_concept_coverage_mode


def _four_kind_index(tmp_path: Path) -> LanceDBSearchIndex:
    index = LanceDBSearchIndex(tmp_path / "search", vector_dimension=2)
    index.upsert([
        SearchChunk("p", "a", "r-a", "父概况", (1.0, 0.0), 5, "MASTER", "上海", "AVAILABLE",
                    keyword_text="父概况", keyword_index_text="父 概况",
                    chunk_type="parent", kind="parent"),
        SearchChunk("pp", "a", "r-a", "画像分点", (0.99, 0.01), 5, "MASTER", "上海", "AVAILABLE",
                    keyword_text="画像分点", keyword_index_text="画像 分点",
                    chunk_type="child", kind="profile_point", parent_id="r-a"),
        SearchChunk("ex", "a", "r-a", "经历片段", (0.98, 0.02), 5, "MASTER", "上海", "AVAILABLE",
                    keyword_text="经历片段", keyword_index_text="经历 片段",
                    chunk_type="child", kind="experience", parent_id="r-a"),
        SearchChunk("pj", "a", "r-a", "项目片段", (0.97, 0.03), 5, "MASTER", "上海", "AVAILABLE",
                    keyword_text="项目片段", keyword_index_text="项目 片段",
                    chunk_type="child", kind="project", parent_id="r-a"),
    ])
    return index


def test_kind_quota_splits_vector_recall_per_kind(tmp_path: Path) -> None:
    """基线是四种 kind 一次取全局 top-K；开启配额后每种 kind 各取 N 行。"""
    index = _four_kind_index(tmp_path)
    baseline = index.search_vector((1.0, 0.0), CandidateFilters(), 10,
                                   channel=CHANNEL_VECTOR_ORIGINAL)
    assert len(baseline) == 1  # 候选人级去重：全局 top-K 只留一个代表行
    assert [signal.rank for signal in baseline[0]["_signals"]] == [1]

    split = index.search_vector((1.0, 0.0), CandidateFilters(), 10,
                                channel=CHANNEL_VECTOR_ORIGINAL, kind_quota=1)
    assert len(split) == 4  # 四种 kind 各一条
    assert {row["kind"] for row in split} == {"parent", "profile_point", "experience", "project"}
    # rank 在各自的 kind 列表内从 1 开始，不跨 kind 累加。
    assert all([signal.rank for signal in row["_signals"]] == [1] for row in split)


def _mixed_channel_index(tmp_path: Path) -> LanceDBSearchIndex:
    index = LanceDBSearchIndex(tmp_path / "search", vector_dimension=2)
    index.upsert([
        SearchChunk("a-p", "a", "r-a", "Java 平台", (0.0, 1.0), 5, "MASTER", "上海", "AVAILABLE",
                    keyword_text="Java 平台", keyword_index_text="java 平台",
                    chunk_type="parent", kind="parent"),
        SearchChunk("b-p", "b", "r-b", "支付平台", (1.0, 0.0), 5, "MASTER", "上海", "AVAILABLE",
                    keyword_text="支付平台", keyword_index_text="支付 平台",
                    chunk_type="parent", kind="parent"),
    ])
    return index


def test_fusion_weights_reach_the_fusion_score(tmp_path: Path, monkeypatch) -> None:
    """BM25 权重与向量权重必须真的进入融合分，而不是只存在于常量里。

    构造「a 只被 BM25 命中、b 只被向量命中」的两条 rank=1 记录，等权时融合分相同，
    改动任一族权重都应只抬升对应候选人的融合分。
    """
    index = _mixed_channel_index(tmp_path)
    fts_rows = [row for row in index.search_fts("Java", CandidateFilters(), 10)
                if row["candidate_id"] == "a"]
    vector_rows = [row for row in index.search_vector((1.0, 0.0), CandidateFilters(), 10,
                                                      channel=CHANNEL_VECTOR_ORIGINAL)
                   if row["candidate_id"] == "b"]
    assert fts_rows and vector_rows

    baseline = {hit.candidate_id: hit.fusion_score for hit in index.fuse(fts_rows, vector_rows, 10)}
    assert baseline["a"] == pytest.approx(1 / 61)
    assert baseline["b"] == pytest.approx(1 / 61)

    monkeypatch.setattr(index_module, "FUSION_WEIGHT_BM25", 1.25)
    lexical = {hit.candidate_id: hit.fusion_score for hit in index.fuse(fts_rows, vector_rows, 10)}
    assert lexical["a"] == pytest.approx(1.25 / 61)
    assert lexical["b"] == pytest.approx(1 / 61)

    monkeypatch.setattr(index_module, "FUSION_WEIGHT_BM25", 1.0)
    monkeypatch.setattr(index_module, "FUSION_WEIGHT_VECTOR", 1.25)
    semantic = {hit.candidate_id: hit.fusion_score for hit in index.fuse(fts_rows, vector_rows, 10)}
    assert semantic["a"] == pytest.approx(1 / 61)
    assert semantic["b"] == pytest.approx(1.25 / 61)


def _hit(candidate_id: str, *, fusion: float, coverage: float) -> SearchHit:
    return SearchHit(chunk_id=f"{candidate_id}-chunk", candidate_id=candidate_id, revision_id=f"r-{candidate_id}",
                     content="x", score=fusion, matched_channels=("bm25",), total_years=None,
                     highest_degree=None, location=None, fusion_score=fusion, concept_coverage=coverage)


def test_concept_coverage_mode_off_keeps_fusion_order(monkeypatch) -> None:
    hits = [_hit("a", fusion=0.5, coverage=0.0), _hit("b", fusion=0.5, coverage=1.0)]
    monkeypatch.setattr(service_module, "CONCEPT_COVERAGE_MODE", "off")
    assert [hit.candidate_id for hit in _apply_concept_coverage_mode(hits)] == ["a", "b"]


def test_concept_coverage_mode_tiebreak_and_light_reorder_only(monkeypatch) -> None:
    """并列时覆盖率高的排前；两档都不改公开 score。"""
    for mode in ("tiebreak", "light"):
        hits = [_hit("a", fusion=0.5, coverage=0.0), _hit("b", fusion=0.5, coverage=1.0)]
        monkeypatch.setattr(service_module, "CONCEPT_COVERAGE_MODE", mode)
        ordered = _apply_concept_coverage_mode(hits)
        assert [hit.candidate_id for hit in ordered] == ["b", "a"]
        assert [hit.score for hit in ordered] == [0.5, 0.5]


def test_concept_coverage_mode_ignores_single_channel_hits(monkeypatch) -> None:
    """关键词模式没有融合分，覆盖率无从比较，必须原样返回。"""
    hits = [SearchHit(chunk_id="a", candidate_id="a", revision_id="r-a", content="x", score=9.0,
                      matched_channels=("bm25",), total_years=None, highest_degree=None,
                      location=None, fusion_score=None, concept_coverage=None),
            SearchHit(chunk_id="b", candidate_id="b", revision_id="r-b", content="y", score=1.0,
                      matched_channels=("bm25",), total_years=None, highest_degree=None,
                      location=None, fusion_score=None, concept_coverage=1.0)]
    monkeypatch.setattr(service_module, "CONCEPT_COVERAGE_MODE", "tiebreak")
    assert [hit.candidate_id for hit in _apply_concept_coverage_mode(hits)] == ["a", "b"]


class _StubEmbedding:
    async def embed_query(self, text: str) -> list[float]:
        return [1.0, 0.0]


class _StubReranker:
    async def rerank(self, query: str, documents: list[str]) -> list[int]:
        return list(range(len(documents)))


def test_hybrid_weak_hit_filter_switch_really_changes_recall(tmp_path: Path, monkeypatch) -> None:
    """阶段 1 旋钮：置 False 时混合通道不再过滤，弱命中行会真的进入融合池。

    若两个档位返回同一批行，说明 A/B 的对照臂是空壳，阶段 1 的定档结论就不成立。
    """
    service = HybridSearchService(index=LanceDBSearchIndex(tmp_path / "search", vector_dimension=2),
                                  embedding_provider=_StubEmbedding(),
                                  reranker_provider=_StubReranker())
    rows = [
        {"candidate_id": "c1", "keyword_index_text": "java spring"},
        {"candidate_id": "c2", "keyword_index_text": "python django"},
    ]

    monkeypatch.setattr(service_module, "HYBRID_WEAK_HIT_FILTER", True)
    diagnostics: dict = {}
    filtered = service._filter_hybrid_fts(rows, "Java Spring", (), diagnostics)
    assert [row["candidate_id"] for row in filtered] == ["c1"]
    assert diagnostics["hybrid_fts_weak_filter"] is True

    monkeypatch.setattr(service_module, "HYBRID_WEAK_HIT_FILTER", False)
    diagnostics = {}
    unfiltered = service._filter_hybrid_fts(rows, "Java Spring", (), diagnostics)
    assert [row["candidate_id"] for row in unfiltered] == ["c1", "c2"]
    assert diagnostics["hybrid_fts_weak_filter"] is False
    assert diagnostics["hybrid_fts_rows_after_concept"] == 2
