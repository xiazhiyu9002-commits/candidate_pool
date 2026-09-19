from pathlib import Path

from kerui_recruit.search.contracts import CandidateFilters, SearchChunk, SearchRequest
from kerui_recruit.search.lancedb_index import LanceDBSearchIndex


def test_hard_filters_never_leak_nonmatching_candidates(tmp_path: Path) -> None:
    """Applying hard filters after top-k retrieval can incorrectly leak or omit candidates."""
    index = LanceDBSearchIndex(tmp_path / "search", vector_dimension=2)
    index.upsert(
        [
            SearchChunk("one", "one", "r1", "Python", (1.0, 0.0), 8, "MASTER", "上海", "AVAILABLE"),
            SearchChunk("two", "two", "r2", "Python", (1.0, 0.0), 3, "MASTER", "上海", "AVAILABLE"),
            SearchChunk("three", "three", "r3", "Python", (1.0, 0.0), 8, "BACHELOR", "上海", "AVAILABLE"),
            SearchChunk("four", "four", "r4", "Python", (1.0, 0.0), 8, "MASTER", "北京", "AVAILABLE"),
        ]
    )

    hits = index.search(
        SearchRequest(
            query="Python",
            query_vector=(1.0, 0.0),
            filters=CandidateFilters(
                min_years=5,
                highest_degree="MASTER",
                location="上海",
                candidate_status="AVAILABLE",
            ),
            limit=100,
        )
    )

    assert [hit.candidate_id for hit in hits] == ["one"]


def test_qs_rank_hard_filter_excludes_high_ranked_candidates(tmp_path: Path) -> None:
    """QS is a hard condition; candidates beyond the cap must be filtered at retrieval time."""
    index = LanceDBSearchIndex(tmp_path / "search", vector_dimension=2)
    index.upsert(
        [
            SearchChunk("one", "one", "r1", "Python", (1.0, 0.0), 5, "MASTER", "上海", "AVAILABLE", 25),
            SearchChunk("two", "two", "r2", "Python", (1.0, 0.0), 5, "MASTER", "上海", "AVAILABLE", 150),
        ]
    )

    hits = index.search(
        SearchRequest(
            query="Python",
            query_vector=(1.0, 0.0),
            filters=CandidateFilters(max_qs_rank=100),
            limit=100,
        )
    )

    assert [hit.candidate_id for hit in hits] == ["one"]


def test_field_filter_matches_beyond_recall_limit(tmp_path: Path) -> None:
    """Field filters must be pushed to the index, never truncated by a fixed recall cap."""
    index = LanceDBSearchIndex(tmp_path / "search", vector_dimension=2)
    chunks = []
    for i in range(250):
        chunks.append(
            SearchChunk(
                id=f"c{i}", candidate_id=f"cand-{i}", revision_id=f"rev-{i}",
                content=f"普通候选人 {i}", vector=(1.0, 0.0),
                total_years=5, highest_degree="BACHELOR", location="上海",
                candidate_status="AVAILABLE", name_terms=(f"候选人{i}",),
            )
        )
    chunks.append(
        SearchChunk(
            id="target", candidate_id="cand-target", revision_id="rev-target",
            content="腾讯科技 产品经理", vector=(1.0, 0.0),
            total_years=5, highest_degree="BACHELOR", location="上海",
            candidate_status="AVAILABLE",
            name_terms=("张三",), company_terms=("腾讯科技",), title_terms=("产品经理",),
        )
    )
    index.upsert(chunks)

    assert [h.candidate_id for h in index.filter_search(CandidateFilters(company="腾讯"), 20)] == ["cand-target"]
    assert [h.candidate_id for h in index.filter_search(CandidateFilters(name="张"), 20)] == ["cand-target"]
    assert [h.candidate_id for h in index.filter_search(CandidateFilters(title="产品"), 20)] == ["cand-target"]


def test_age_range_filter(tmp_path: Path) -> None:
    index = LanceDBSearchIndex(tmp_path / "search", vector_dimension=2)
    index.upsert([
        SearchChunk("a", "a", "r1", "Python", (1.0, 0.0), 5, "BACHELOR", "上海", "AVAILABLE", age=25),
        SearchChunk("b", "b", "r2", "Python", (1.0, 0.0), 5, "BACHELOR", "上海", "AVAILABLE", age=40),
    ])
    hits = index.filter_search(CandidateFilters(min_age=30, max_age=50), 20)
    assert [h.candidate_id for h in hits] == ["b"]


def test_candidate_ids_filter_narrows_before_ranking(tmp_path: Path) -> None:
    """手机号/性别下沉后传入的候选人集合必须在索引层先缩小，而非召回后再过滤。"""
    index = LanceDBSearchIndex(tmp_path / "search", vector_dimension=2)
    index.upsert([
        SearchChunk("one", "cand-1", "r1", "Python", (1.0, 0.0), 5, "BACHELOR", "上海", "AVAILABLE"),
        SearchChunk("two", "cand-2", "r2", "Python", (1.0, 0.0), 5, "BACHELOR", "上海", "AVAILABLE"),
        SearchChunk("three", "cand-3", "r3", "Python", (1.0, 0.0), 5, "BACHELOR", "上海", "AVAILABLE"),
    ])
    hits = index.filter_search(CandidateFilters(candidate_ids=("cand-2",)), 20)
    assert [h.candidate_id for h in hits] == ["cand-2"]
