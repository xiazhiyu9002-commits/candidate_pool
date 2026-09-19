from pathlib import Path

from kerui_recruit.search.contracts import CandidateFilters, SearchChunk, SearchRequest
from kerui_recruit.search.lancedb_index import LanceDBSearchIndex


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
