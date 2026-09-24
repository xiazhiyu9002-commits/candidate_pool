from pathlib import Path

import pytest

from kerui_recruit.search.contracts import CandidateFilters, SearchChunk, SearchRequest
from kerui_recruit.search.lancedb_index import (
    CHANNEL_VECTOR_ORIGINAL,
    CHANNEL_VECTOR_REWRITE,
    LanceDBSearchIndex,
)


def chunk(
    candidate_id: str,
    content: str,
    vector: tuple[float, float],
    *,
    revision_id: str = "revision-1",
    total_years: float = 5,
    degree: str = "BACHELOR",
) -> SearchChunk:
    return SearchChunk(
        id=f"chunk-{candidate_id}",
        candidate_id=candidate_id,
        revision_id=revision_id,
        content=content,
        vector=vector,
        total_years=total_years,
        highest_degree=degree,
        location="上海",
        candidate_status="AVAILABLE",
    )


def test_hybrid_search_combines_keyword_and_semantic_evidence(tmp_path: Path) -> None:
    """Removing either retrieval channel must change and degrade the expected ranking."""
    index = LanceDBSearchIndex(tmp_path / "search", vector_dimension=2)
    index.upsert(
        [
            chunk("a", "Java payment platform", (1.0, 0.0)),
            chunk("b", "financial settlement platform", (0.9, 0.1)),
            chunk("c", "graphic design", (0.0, 1.0)),
        ]
    )

    hits = index.search(
        SearchRequest(
            query="Java finance",
            query_vector=(1.0, 0.0),
            filters=CandidateFilters(),
            limit=20,
        )
    )

    assert [hit.candidate_id for hit in hits[:2]] == ["a", "b"]
    assert hits[0].matched_channels == ("bm25", "vector")


def test_deleting_a_revision_removes_all_of_its_chunks(tmp_path: Path) -> None:
    """An obsolete resume revision must never remain searchable after replacement."""
    index = LanceDBSearchIndex(tmp_path / "search", vector_dimension=2)
    index.upsert([chunk("a", "Python", (1.0, 0.0), revision_id="old")])

    index.delete_revision("old")
    hits = index.search(
        SearchRequest("Python", (1.0, 0.0), CandidateFilters(), limit=20)
    )

    assert hits == []


def test_candidate_fusion_merges_parent_and_child_channels(tmp_path: Path) -> None:
    """父 chunk 命中 FTS、子 chunk 命中向量时，候选人应同时具备两通道证据。"""
    index = LanceDBSearchIndex(tmp_path / "search", vector_dimension=2)
    index.upsert(
        [
            SearchChunk("a-parent", "a", "r-a", "Java 后端", (0.0, 1.0), 5, "BACHELOR", "上海", "AVAILABLE",
                        keyword_text="Java 后端", keyword_index_text="java 后端", chunk_type="parent"),
            SearchChunk("a-child", "a", "r-a", "支付服务", (1.0, 0.0), 5, "BACHELOR", "上海", "AVAILABLE",
                        keyword_text="支付服务", keyword_index_text="支付服务", chunk_type="child", parent_id="r-a"),
            SearchChunk("b-parent", "b", "r-b", "Java finance", (1.0, 0.0), 5, "BACHELOR", "上海", "AVAILABLE",
                        keyword_text="Java finance", keyword_index_text="java finance", chunk_type="parent"),
        ]
    )
    hits = index.search(
        SearchRequest("Java finance", (1.0, 0.0), CandidateFilters(), limit=20)
    )
    a = next(h for h in hits if h.candidate_id == "a")
    assert set(a.matched_channels) == {"bm25", "vector"}


def test_many_child_chunks_do_not_crowd_out_other_candidate(tmp_path: Path) -> None:
    """一个候选人 20 个子 chunk 不应挤走另一个独立候选人的融合名次。"""
    index = LanceDBSearchIndex(tmp_path / "search", vector_dimension=2)
    chunks = [
        SearchChunk(f"a-child-{i}", "a", "r-a", f"Java {i}", (1.0, 0.0), 5, "BACHELOR", "上海", "AVAILABLE",
                    keyword_text=f"Java {i}", keyword_index_text=f"java {i}", chunk_type="child", parent_id="r-a")
        for i in range(20)
    ]
    chunks.append(
        SearchChunk("b-parent", "b", "r-b", "Java backend", (0.9, 0.1), 5, "BACHELOR", "上海", "AVAILABLE",
                    keyword_text="Java backend", keyword_index_text="java backend", chunk_type="parent")
    )
    index.upsert(chunks)
    hits = index.search(SearchRequest("Java", (1.0, 0.0), CandidateFilters(), limit=50))
    candidates = [h.candidate_id for h in hits]
    assert "a" in candidates
    assert "b" in candidates
    # b 不应被 a 的 20 个子 chunk 挤到很靠后。
    assert candidates.index("b") < 5


def _parent_and_child_index(tmp_path: Path) -> LanceDBSearchIndex:
    index = LanceDBSearchIndex(tmp_path / "search", vector_dimension=2)
    index.upsert([
        SearchChunk("a-parent", "a", "r-a", "父概况文本", (0.0, 1.0), 5, "BACHELOR", "上海", "AVAILABLE",
                    keyword_text="父概况文本", keyword_index_text="父 概况 文本",
                    chunk_type="parent", kind="parent"),
        SearchChunk("a-child", "a", "r-a", "支付服务", (1.0, 0.0), 5, "BACHELOR", "上海", "AVAILABLE",
                    keyword_text="支付服务", keyword_index_text="支付 服务",
                    chunk_type="child", kind="project", parent_id="r-a"),
    ])
    return index


def test_representative_fields_are_projections_of_the_real_parent(tmp_path: Path) -> None:
    """向量只命中 child 时，展示字段仍批量回读真实 parent，且 child 进入证据包。"""
    index = _parent_and_child_index(tmp_path)
    vector_rows = index.search_vector((1.0, 0.0), CandidateFilters(), 10,
                                      channel=CHANNEL_VECTOR_ORIGINAL)
    hits = index.fuse([], vector_rows, 10)
    assert len(hits) == 1
    assert hits[0].chunk_id == "a-parent"
    assert hits[0].content == "父概况文本"
    assert hits[0].representative_kind == "parent"
    assert hits[0].matched_channels == ("vector",)
    assert hits[0].vector_original_rank == 1
    assert any(chunk.kind == "project" for chunk in hits[0].evidence)


def test_bm25_hits_parent_and_vector_hits_child_for_the_same_candidate(tmp_path: Path) -> None:
    """BM25 命中父行、向量命中 child：展示字段取真实 parent，两通道与两条证据都保留。"""
    index = LanceDBSearchIndex(tmp_path / "search", vector_dimension=2)
    index.upsert([
        SearchChunk("a-parent", "a", "r-a", "Java 后端概况", (0.0, 1.0), 5, "BACHELOR", "上海", "AVAILABLE",
                    keyword_text="Java 后端概况", keyword_index_text="java 后端 概况",
                    chunk_type="parent", kind="parent"),
        SearchChunk("a-child", "a", "r-a", "支付清结算服务", (1.0, 0.0), 5, "BACHELOR", "上海", "AVAILABLE",
                    keyword_text="支付清结算服务", keyword_index_text="支付 清结算 服务",
                    chunk_type="child", kind="experience", parent_id="r-a"),
        SearchChunk("b-parent", "b", "r-b", "Java 平台概况", (1.0, 0.0), 5, "BACHELOR", "上海", "AVAILABLE",
                    keyword_text="Java 平台概况", keyword_index_text="java 平台 概况",
                    chunk_type="parent", kind="parent"),
    ])
    fts_rows = index.search_fts("Java", CandidateFilters(), 10)
    vector_rows = index.search_vector((1.0, 0.0), CandidateFilters(), 10,
                                      channel=CHANNEL_VECTOR_ORIGINAL)
    hits = index.fuse(fts_rows, vector_rows, 10)
    a = next(hit for hit in hits if hit.candidate_id == "a")
    assert a.chunk_id == "a-parent"
    assert a.content == "Java 后端概况"
    assert a.vector_text == "Java 后端概况"  # 取自真实 parent，不是触发召回的 child
    assert set(a.matched_channels) == {"bm25", "vector"}
    assert a.bm25_rank == 1
    assert a.vector_original_rank is not None
    kinds = {chunk.kind for chunk in a.evidence}
    assert {"parent", "experience"} <= kinds
    child = next(chunk for chunk in a.evidence if chunk.kind == "experience")
    assert [signal.channel for signal in child.signals] == [CHANNEL_VECTOR_ORIGINAL]
    assert child.chunk_id == "a-child"


def test_fusion_is_invariant_to_channel_traversal_order(tmp_path: Path) -> None:
    """交换通道遍历顺序不改变排序、通道并集与代表字段。"""
    index = _parent_and_child_index(tmp_path)
    fts_rows = index.search_fts("支付", CandidateFilters(), 10, search_body=True)
    vector_rows = index.search_vector((1.0, 0.0), CandidateFilters(), 10,
                                      channel=CHANNEL_VECTOR_ORIGINAL)
    assert fts_rows and vector_rows

    forward = index.fuse(fts_rows, vector_rows, 10)
    # 参数位置对调：通道身份来自行的 ChannelSignal，而不是列表位置。
    reverse = index.fuse(vector_rows, fts_rows, 10)

    def signature(hits):
        return [(hit.candidate_id, hit.chunk_id, hit.content, hit.matched_channels,
                 round(hit.fusion_score or 0.0, 9)) for hit in hits]

    assert signature(forward) == signature(reverse)


def test_vector_family_takes_the_maximum_contribution(tmp_path: Path) -> None:
    """原查询与改写查询命中同一候选人时取最大值，避免双倍向量权重。"""
    index = LanceDBSearchIndex(tmp_path / "search", vector_dimension=2)
    index.upsert([
        SearchChunk(f"{name}-parent", name, f"r-{name}", f"{name} 概况", (1.0, 0.0), 5,
                    "BACHELOR", "上海", "AVAILABLE", keyword_text=f"{name} 概况",
                    keyword_index_text=f"{name} 概况", chunk_type="parent", kind="parent")
        for name in ("a", "b")
    ])
    original = index.search_vector((1.0, 0.0), CandidateFilters(), 10, channel=CHANNEL_VECTOR_ORIGINAL)
    rewrite = index.search_vector((1.0, 0.0), CandidateFilters(), 10, channel=CHANNEL_VECTOR_REWRITE)
    hits = index.fuse([], original, 10, rewrite)
    # 每路贡献都是 1/(rrf_k+rank)；两路同时命中取 max 而不是相加（否则会是 2/61、2/62）。
    assert sorted(hit.fusion_score for hit in hits) == pytest.approx([1 / 62, 1 / 61])
    # 两路的 rank 各自保留，且完全相同（同一份结果喂给两个通道）。
    assert all(hit.vector_original_rank == hit.vector_rewrite_rank for hit in hits)


def test_hybrid_fusion_uses_channel_internal_ranks(tmp_path: Path) -> None:
    """融合分 = w_fts × rr(bm25) + w_vector × rr(vector)，不比较跨通道 raw rank。"""
    index = _parent_and_child_index(tmp_path)
    fts_rows = index.search_fts("支付 服务", CandidateFilters(), 10, search_body=True)
    vector_rows = index.search_vector((1.0, 0.0), CandidateFilters(), 10,
                                      channel=CHANNEL_VECTOR_ORIGINAL)
    assert fts_rows and vector_rows
    hits = index.fuse(fts_rows, vector_rows, 10)
    assert hits[0].bm25_rank == 1
    assert hits[0].vector_original_rank == 1
    assert hits[0].fusion_score == pytest.approx(2 / 61)
    assert hits[0].bm25_score is not None
    assert hits[0].vector_original_score == pytest.approx(1.0)
