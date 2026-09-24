"""任务组 8：可筛字段「先判断能不能硬筛，不能就退化为软排」。

判据是**失败开放**：硬筛不得把结果清空。与 ``_apply_rerank_min_score``、
``_apply_vector_absolute_fallback``、混合检索的概念闸门同一条纪律——
质量闸门用于压尾部噪声，不能把「非空」变「空」。
"""
from __future__ import annotations

import time

import pytest

from kerui_recruit.providers.fakes import FakeRerankerProvider
from kerui_recruit.search.contracts import CandidateFilters, SearchChunk
from kerui_recruit.search.lancedb_index import LanceDBSearchIndex
from kerui_recruit.search.service import HybridSearchService


def chunk(cid, content="Python", **kwargs):
    return SearchChunk(f"{cid}-0", cid, f"r-{cid}", content, (1., 0.), 5,
                       "MASTER", "上海", "AVAILABLE", **kwargs)


class Embedding:
    async def embed_query(self, text):
        return [1., 0.]


def service(index, timeout=2):
    return HybridSearchService(index=index, embedding_provider=Embedding(),
                               reranker_provider=FakeRerankerProvider(), search_timeout=timeout)


@pytest.fixture()
def index(tmp_path):
    index = LanceDBSearchIndex(tmp_path, vector_dimension=2)
    index.upsert([
        chunk("a", "Java 后端", company_terms=("字节跳动",), title_terms=("Java 工程师",),
              career_directions=("TECH_BACKEND",)),
        chunk("b", "Python 算法", company_terms=("美团",), title_terms=("算法工程师",),
              career_directions=("TECH_ALGORITHM",)),
    ])
    return index


@pytest.mark.asyncio
async def test_matching_hard_filter_is_kept(index):
    """能筛出人 → 保留硬筛，一次探测就收工（不产生任何退化记录）。"""
    page = await service(index).search(
        "", CandidateFilters(company="字节跳动"), limit=5,
        relaxable_fields=("company",))

    assert [hit.candidate_id for hit in page.items] == ["a"]
    assert page.relaxed == ()
    assert page.effective_filters is None


@pytest.mark.asyncio
async def test_unmatchable_filter_degrades_and_keeps_results(index):
    """筛不出人 → 退化：条件离开 where，但结果照常返回（绝不把非空变空）。"""
    page = await service(index).search(
        "", CandidateFilters(company="并不存在的公司"), limit=5,
        relaxable_fields=("company",))

    assert page.relaxed == ("company",)
    assert {hit.candidate_id for hit in page.items} == {"a", "b"}
    # 调用方必须拿到**实际生效**的条件，否则后续实时校验会把条件再加回去。
    assert page.effective_filters is not None
    assert page.effective_filters.company is None
    assert "FILTER_RELAXED:company" in page.degraded_reasons


@pytest.mark.asyncio
async def test_not_relaxable_field_still_yields_empty(index):
    """面板手填的值不在 relaxable_fields 里：筛空就是筛空，不做任何放宽。"""
    page = await service(index).search(
        "", CandidateFilters(company="并不存在的公司"), limit=5)

    assert page.items == ()
    assert page.relaxed == ()
    assert page.effective_filters is None


@pytest.mark.asyncio
async def test_no_single_relaxation_is_enough_returns_empty_instead_of_widening(index):
    """丢哪一条都还是空 → 什么都不动、如实返回空。

    原先这里会**连环丢**到剩下某个条件非空为止（结果是「命中 TECH_NOPE 方向」的 0 条，
    于是两臂都空；而在真实语料上它会返回一批只满足那个模糊方向的人）。8.5 补测实测：
    救回的名单与「只去掉那条附加条件本该得到的名单」重合度@10 中位数 0.000
    —— 数量救回来了，问题没被回答。用户 2026-09-22 选定「只丢一条 + 优先丢不具体的」。
    """
    page = await service(index).search(
        "", CandidateFilters(company="并不存在的公司",
                             career_directions=("TECH_NOPE",)),
        limit=5, relaxable_fields=("company", "career_directions"))

    assert page.relaxed == ()
    assert page.items == ()
    assert page.effective_filters is None


@pytest.mark.asyncio
async def test_single_field_probe_finds_the_culprit_instead_of_dropping_the_satisfiable_one(index):
    """单字段试，而不是按顺序连环丢：要丢掉真正筛空的那条，保住能满足的那条。

    语料里只有「技术方向 = TECH_BACKEND」的人（`a`）。[company=不存在, 方向=TECH_BACKEND]
    两条一起筛空——按 `_RELAXATION_ORDER` 顺序先丢方向的话，剩下 [company=不存在] 依旧是空，
    等于把「本来能满足的那条」丢了。这里必须退 company、保留方向。
    """
    page = await service(index).search(
        "", CandidateFilters(company="并不存在的公司",
                             career_directions=("TECH_BACKEND",)),
        limit=5, relaxable_fields=("company", "career_directions"))

    assert page.relaxed == ("company",)
    assert page.effective_filters.career_directions == ("TECH_BACKEND",)
    assert [hit.candidate_id for hit in page.items] == ["a"]


@pytest.mark.asyncio
async def test_least_specific_field_degrades_first(index):
    """多条都「丢掉就能筛出人」时，先退**最不具体**的那条（枚举），保住 company/title。

    这是 8.5 补测的核心结论：company/title 是最具体、使用者最在意的条件，
    而原先的优先级把 company 排在最前面先丢。
    """
    page = await service(index).search(
        "", CandidateFilters(career_directions=("TECH_BACKEND",),
                             title="并不存在的职位"),
        limit=5, relaxable_fields=("career_directions", "title"))

    # 丢方向 → [title=不存在] 空；丢 title → [方向=TECH_BACKEND] 有 a。只有一条成立。
    assert page.relaxed == ("title",)
    assert page.effective_filters.title is None
    assert page.effective_filters.career_directions == ("TECH_BACKEND",)
    assert [hit.candidate_id for hit in page.items] == ["a"]


@pytest.mark.asyncio
async def test_degraded_filter_keeps_ranking_signal_via_query(index):
    """退化不是「丢掉条件」：词条仍在召回链里，软排信号照常参与打分。"""
    page = await service(index).search(
        "算法", CandidateFilters(company="并不存在的公司"), limit=5,
        relaxable_fields=("company",))

    assert page.relaxed == ("company",)
    assert "b" in {hit.candidate_id for hit in page.items}


@pytest.mark.asyncio
async def test_probe_failure_never_relaxes(index):
    """探测本身失败 → 什么都不退化。没有证据就放宽条件，等于悄悄丢掉使用者的要求。"""
    svc = service(index)

    def boom(*args, **kwargs):
        raise RuntimeError("index unavailable")

    index.filter_search = boom
    filters = CandidateFilters(company="并不存在的公司")
    effective, relaxed = await svc._relax_unmatchable_filters(
        filters, ("company",), budget=time.monotonic() + 1)

    assert relaxed == ()
    assert effective == filters
