"""S4 索引级验证：证据包在不同检索通道下取到的片段。

- 向量通道返回全部 chunk（parent + 子片段），证据包应含多个 kind；
- FTS 通道按设计只检索 parent chunk（``chunk_type = 'parent'``），因此证据包只有一条。
"""
from __future__ import annotations

from pathlib import Path

from kerui_recruit.search.contracts import CandidateFilters, SearchChunk
from kerui_recruit.search.lancedb_index import LanceDBSearchIndex


def _index(tmp_path: Path) -> LanceDBSearchIndex:
    index = LanceDBSearchIndex(tmp_path / "candidates", vector_dimension=2)
    index.upsert([
        SearchChunk(id="p1", candidate_id="person", revision_id="rev", content="整体画像",
                    vector=(1.0, 0.0), total_years=8.0, highest_degree="BACHELOR", location="上海",
                    candidate_status="AVAILABLE", keyword_text="整体画像",
                    keyword_index_text="整体 画像", chunk_type="parent", kind="parent"),
        SearchChunk(id="c1", candidate_id="person", revision_id="rev", content="用 RAG 搭建客服",
                    vector=(0.99, 0.01), total_years=8.0, highest_degree="BACHELOR", location="上海",
                    candidate_status="AVAILABLE", keyword_text="用 RAG 搭建客服",
                    keyword_index_text="用 rag 搭建 客服", chunk_type="child", kind="project"),
        SearchChunk(id="c2", candidate_id="person", revision_id="rev", content="负责保险理赔系统",
                    vector=(0.98, 0.02), total_years=8.0, highest_degree="BACHELOR", location="上海",
                    candidate_status="AVAILABLE", keyword_text="负责保险理赔系统",
                    keyword_index_text="负责 保险 理赔 系统", chunk_type="child", kind="experience"),
    ])
    return index


def test_vector_channel_evidence_keeps_multiple_kinds(tmp_path: Path) -> None:
    index = _index(tmp_path)
    hits = index.hits_from_rows(index.search_vector((1.0, 0.0), CandidateFilters(), 5), "vector")
    assert len(hits) == 1
    kinds = [chunk.kind for chunk in hits[0].evidence]
    assert len(kinds) == 3
    assert len(set(kinds)) == 3


def test_fts_channel_evidence_contains_parent_only(tmp_path: Path) -> None:
    """FTS 只检索父 chunk（既有设计），因此词法通道下证据包只有一条。"""
    index = _index(tmp_path)
    rows = index.search_fts("整体 画像", CandidateFilters(), 5)
    hits = index.hits_from_rows(rows, "bm25")
    assert len(hits) == 1
    assert [chunk.kind for chunk in hits[0].evidence] == ["parent"]


def test_evidence_is_empty_when_index_has_single_chunk(tmp_path: Path) -> None:
    index = LanceDBSearchIndex(tmp_path / "solo", vector_dimension=2)
    index.upsert([
        SearchChunk(id="p1", candidate_id="person", revision_id="rev", content="整体画像",
                    vector=(1.0, 0.0), total_years=8.0, highest_degree="BACHELOR", location="上海",
                    candidate_status="AVAILABLE", keyword_text="整体画像",
                    keyword_index_text="整体 画像", chunk_type="parent", kind="parent"),
    ])
    hits = index.hits_from_rows(index.search_vector((1.0, 0.0), CandidateFilters(), 5), "vector")
    assert len(hits[0].evidence) == 1
