"""S4 证据包单测：索引侧片段挑选 + 服务侧「父画像 / 技术 / 业务」分类。"""
from __future__ import annotations

from types import SimpleNamespace

from kerui_recruit.match.service import _EVIDENCE_TEXT_LIMIT, MatchService, _evidence_pack
from kerui_recruit.search.contracts import ChannelSignal, EvidenceChunk, SearchHit
from kerui_recruit.search.lancedb_index import select_evidence


def _row(text: str, kind: str, score: float = 1.0, chunk_type: str | None = None) -> dict:
    row = {"keyword_text": text, "kind": kind, "_score": score}
    if chunk_type is not None:
        row["chunk_type"] = chunk_type
    return row


def _hit(evidence: tuple[EvidenceChunk, ...]) -> SearchHit:
    return SearchHit(chunk_id="c1", candidate_id="cand-1", revision_id="rev-1", content="x",
                     score=1.0, matched_channels=("bm25",), total_years=5.0,
                     highest_degree="BACHELOR", location="上海", evidence=evidence)


def test_select_evidence_keeps_first_row_as_representative() -> None:
    rows = [_row("父画像文本", "parent"), _row("项目 A 片段", "project")]
    picked = select_evidence(rows)
    assert [chunk.kind for chunk in picked] == ["parent", "project"]
    assert picked[0].text == "父画像文本"


def test_select_evidence_allows_two_chunks_of_the_same_kind() -> None:
    """去重口径是文本而不是 kind：同 kind 的两条不同片段可以同时保留。"""
    rows = [_row("父画像", "parent"), _row("项目 A", "project"), _row("项目 B", "project"),
            _row("经历 C", "experience")]
    picked = select_evidence(rows)
    assert [chunk.kind for chunk in picked] == ["parent", "project", "project"]


def test_select_evidence_prefers_higher_rank_contribution() -> None:
    """带通道信号时按最大贡献排序，不受输入顺序影响。"""
    rows = [_row("低贡献片段", "experience"), _row("高贡献片段", "project")]
    rows[0]["_signals"] = (ChannelSignal(channel="bm25", rank=9, reciprocal_rank=1 / 69),)
    rows[1]["_signals"] = (ChannelSignal(channel="bm25", rank=1, reciprocal_rank=1 / 61),)
    picked = select_evidence(rows)
    assert [chunk.text for chunk in picked] == ["高贡献片段", "低贡献片段"]
    assert picked[0].signals[0].channel == "bm25"


def test_select_evidence_skips_blank_and_duplicate_text() -> None:
    rows = [_row("   ", "parent"), _row("同一段文本", "parent"),
            _row("同一段文本", "project"), _row("不同文本", "experience")]
    picked = select_evidence(rows)
    assert [chunk.text for chunk in picked] == ["同一段文本", "不同文本"]


def test_select_evidence_respects_limit_and_empty_input() -> None:
    rows = [_row(f"片段{i}", f"kind{i}") for i in range(6)]
    assert len(select_evidence(rows)) == 3
    assert select_evidence([]) == ()


def test_evidence_pack_classifies_tech_and_business() -> None:
    """真实 parent 进 overview；命中 JD 技术词 / 业务词的片段分别进 tech / business。"""
    pack = _evidence_pack(
        _hit((
            EvidenceChunk(kind="parent", text="整体画像：8 年后端"),
            EvidenceChunk(kind="project", text="用 RAG 与 LangGraph 搭建智能客服"),
            EvidenceChunk(kind="experience", text="负责保险理赔核心系统改造"),
        )),
        {"required_skills": ["RAG"], "business_directions": ["INSURANCE"]},
        {"skills": ["LangGraph"], "business_directions": ["INSURANCE"]},
    )
    assert pack["overview"] == "整体画像：8 年后端"
    assert "RAG" in pack["tech"]
    assert "保险理赔" in pack["business"]


def test_evidence_pack_overview_is_real_parent_not_first_chunk() -> None:
    """首条不是 parent 时，overview 必须取真实 parent，不能把 child 伪装成 parent。"""
    pack = _evidence_pack(
        _hit((
            EvidenceChunk(kind="project", text="用 RAG 搭建客服"),
            EvidenceChunk(kind="parent", text="整体画像：8 年后端"),
        )),
        {"required_skills": ["RAG"]}, {},
    )
    assert pack["overview"] == "整体画像：8 年后端"
    assert "RAG" in pack["tech"]


def test_evidence_pack_without_real_parent_falls_back_without_relabelling() -> None:
    """索引里确实没有 parent 时按贡献回退，且不把回退片段标成 parent 槽的来源。"""
    pack = _evidence_pack(
        _hit((
            EvidenceChunk(kind="experience", text="负责保险理赔系统"),
            EvidenceChunk(kind="project", text="用 RAG 搭建客服"),
        )),
        {"required_skills": ["RAG"]}, {},
    )
    assert pack["overview"] in ("负责保险理赔系统", "用 RAG 搭建客服")
    assert "RAG" in pack["tech"]


def test_evidence_pack_without_evidence_is_empty() -> None:
    assert _evidence_pack(_hit(()), {"required_skills": ["Java"]}, {}) == {}


def test_evidence_pack_truncates_long_text() -> None:
    long_text = "技术" * 400
    pack = _evidence_pack(
        _hit((EvidenceChunk(kind="parent", text=long_text),)),
        {"required_skills": ["Java"]}, {},
    )
    assert pack["overview"].endswith("…")
    assert len(pack["overview"]) == _EVIDENCE_TEXT_LIMIT + 1


def test_evidence_pack_only_overview_when_no_child_matches_terms() -> None:
    pack = _evidence_pack(
        _hit((EvidenceChunk(kind="parent", text="父画像"),
              EvidenceChunk(kind="project", text="与任何词表都不重叠的片段"))),
        {"required_skills": ["Java"]}, {},
    )
    assert set(pack) == {"overview"}


# ---- 定向补齐（混合模式的向量阈值会丢掉子片段）----


class _IndexWithChunks:
    def __init__(self, rows: dict[str, list[dict]]) -> None:
        self.rows = rows

    def get_revision_chunks(self, revision_id: str) -> list[dict]:
        return self.rows.get(revision_id, [])


def _service(index) -> MatchService:
    return MatchService(session_factory=None, search_service=SimpleNamespace(index=index))


def test_attach_evidence_pulls_child_chunks_by_revision() -> None:
    index = _IndexWithChunks({"rev-1": [
        _row("父画像文本", "parent"),
        _row("用 RAG 搭建客服", "project"),
        _row("负责保险理赔系统", "experience"),
    ]})
    enriched = _service(index)._attach_evidence(_hit(()))
    assert [chunk.kind for chunk in enriched.evidence] == ["parent", "project", "experience"]
    # 概况取索引里的真实 parent，而不是召回命中的那一行文本。
    assert enriched.evidence[0].text == "父画像文本"


def test_attach_evidence_without_parent_row_keeps_representative_kind() -> None:
    """索引里没有 parent 行时，概况回退保留候选人的真实 kind，不伪装成 parent。"""
    index = _IndexWithChunks({"rev-1": [_row("项目片段", "project")]})
    hit = SearchHit(chunk_id="c1", candidate_id="cand-1", revision_id="rev-1", content="项目片段",
                    score=1.0, matched_channels=("vector",), total_years=5.0,
                    highest_degree="BACHELOR", location="上海",
                    representative_kind="project", evidence=())
    enriched = _service(index)._attach_evidence(hit, terms={"项目"})
    assert enriched.evidence[0].kind == "project"


def test_attach_evidence_recognises_jd_child_rows_by_chunk_type() -> None:
    """岗位索引的 kind 恒为 parent，只有 chunk_type 区分父子，必须按 chunk_type 识别。"""
    index = _IndexWithChunks({"rev-1": [
        _row("岗位整体画像", "parent", chunk_type="parent"),
        _row("精通 Java 与 Spring", "parent", chunk_type="child"),
    ]})
    enriched = _service(index)._attach_evidence(_hit(()), terms={"java", "spring"})
    assert [chunk.kind for chunk in enriched.evidence] == ["parent", "child"]
    assert "Java" in enriched.evidence[1].text


def test_attach_evidence_ranks_children_by_term_overlap() -> None:
    index = _IndexWithChunks({"rev-1": [
        _row("父画像", "parent"),
        _row("与词表无关的经历片段", "experience"),
        _row("用 RAG 与 LangGraph 做智能客服", "project"),
    ]})
    enriched = _service(index)._attach_evidence(_hit(()), terms={"rag", "langgraph"})
    # 只保留与目标词有命中的片段，并排在前面。
    assert [chunk.text for chunk in enriched.evidence[1:]] == ["用 RAG 与 LangGraph 做智能客服"]


def test_attach_evidence_is_noop_without_index_support() -> None:
    """假索引/FakeIndex 没有 get_revision_chunks 时原样返回，不影响既有测试与降级路径。"""
    hit = _hit((EvidenceChunk(kind="parent", text="父画像"),))
    assert _service(object())._attach_evidence(hit) == hit


def test_enrich_evidence_respects_cap() -> None:
    index = _IndexWithChunks({"rev-1": [_row("父画像", "parent"), _row("项目", "project")]})
    service = _service(index)
    hits = [SearchHit(chunk_id=f"c{i}", candidate_id=f"cand-{i}", revision_id="rev-1",
                      content="x", score=1.0, matched_channels=("bm25",), total_years=5.0,
                      highest_degree="BACHELOR", location="上海") for i in range(5)]
    enriched = service._enrich_evidence(hits, 2, set())
    assert all(len(hit.evidence) == 2 for hit in enriched[:2])
    assert all(hit.evidence == () for hit in enriched[2:])
