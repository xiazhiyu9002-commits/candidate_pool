"""搜索侧证据槽位选择：概况 + 查询主证据 + 查询补充证据。

搜索侧刻意不区分「技术 / 业务」概念（技能词表与行业/业务方向标签粒度不同，
临时拼成二分类会产生多标签歧义），只用 ``parse_query`` 的全部概念做覆盖比较。
"""
from __future__ import annotations

from kerui_recruit.search.contracts import EvidenceChunk
from kerui_recruit.search.lexicon import concepts_from_query
from kerui_recruit.search.service import (
    SEARCH_EVIDENCE_SLOTS,
    render_evidence_pack,
    search_evidence_pack,
)


def _chunk(kind: str, text: str, *, rank: int = 1, channel: str = "bm25") -> EvidenceChunk:
    from kerui_recruit.search.contracts import ChannelSignal

    return EvidenceChunk(
        kind=kind,
        text=text,
        chunk_id=f"{kind}:{text[:4]}",
        signals=(ChannelSignal(channel=channel, rank=rank, reciprocal_rank=1.0 / (60 + rank)),),
    )


def _pack(evidence, query: str) -> dict[str, str]:
    return search_evidence_pack(evidence, concepts_from_query(query))


def test_overview_is_real_parent_even_when_not_first() -> None:
    pack = _pack((
        _chunk("project", "用 RAG 搭建智能客服"),
        _chunk("parent", "整体概况：8 年后端"),
    ), "RAG")
    assert pack["overview"] == "整体概况：8 年后端"


def test_primary_maximises_query_concept_coverage() -> None:
    """主证据取覆盖查询概念最多的片段，而不是排名最高的那一段。"""
    pack = _pack((
        _chunk("parent", "整体概况"),
        _chunk("project", "只提到 Kafka", rank=1),
        _chunk("experience", "用 Kafka 与 Redis 做支付清结算", rank=2),
    ), "Kafka Redis 支付")
    assert pack["overview"] == "整体概况"
    assert "Redis" in pack["primary"]


def test_complementary_covers_concepts_the_primary_missed() -> None:
    pack = _pack((
        _chunk("parent", "整体概况"),
        _chunk("experience", "用 Kafka 做支付", rank=1),
        _chunk("project", "用 Redis 做缓存", rank=2),
        _chunk("project", "与查询词无关的片段", rank=3),
    ), "Kafka Redis 支付")
    assert "Kafka" in pack["primary"]
    assert "Redis" in pack["complementary"]


def test_two_chunks_of_the_same_kind_can_both_be_selected() -> None:
    """槽位不要求 kind 不同，只要求文本不同。"""
    pack = _pack((
        _chunk("parent", "整体概况"),
        _chunk("profile_point", "用 Kafka 做支付", rank=1),
        _chunk("profile_point", "用 Redis 做缓存", rank=2),
    ), "Kafka Redis")
    assert "Kafka" in pack["primary"]
    assert "Redis" in pack["complementary"]


def test_vector_only_chunk_without_lexical_coverage_still_fills_slots() -> None:
    """没有词法覆盖但经向量阈值进入召回的片段，仍按贡献参与兜底。"""
    pack = _pack((
        _chunk("parent", "整体概况"),
        _chunk("experience", "自然语言职责表述，词表未收录", rank=1, channel="vector_original"),
    ), "Kafka")
    assert pack["primary"] == "自然语言职责表述，词表未收录"


def test_missing_parent_keeps_original_kind_and_falls_back_by_contribution() -> None:
    pack = _pack((
        _chunk("experience", "低贡献片段", rank=5),
        _chunk("project", "高贡献片段", rank=1),
    ), "Kafka")
    assert pack["overview"] == "高贡献片段"


def test_empty_evidence_yields_empty_pack() -> None:
    assert search_evidence_pack((), concepts_from_query("Java")) == {}
    assert search_evidence_pack((EvidenceChunk(kind="parent", text="   "),), ()) == {}
    assert render_evidence_pack({}) == ""


def test_render_includes_slot_headers_in_fixed_order() -> None:
    rendered = render_evidence_pack({"overview": "概况", "primary": "主证据", "complementary": "补充"})
    assert rendered.splitlines()[0] == "[概况]"
    assert "[查询主证据]" in rendered
    assert "[查询补充证据]" in rendered
    assert list(SEARCH_EVIDENCE_SLOTS) == ["overview", "primary", "complementary"]
