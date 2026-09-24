"""AI 查询解析（search/parse.py）的逐字段校验、回退链、缓存与方向 A+C 交叉验证。"""
import asyncio
from pathlib import Path

import pytest

from kerui_recruit.providers.fakes import FakeRerankerProvider
from kerui_recruit.search.contracts import CandidateFilters
from kerui_recruit.search.parse import ParsedSearchPlan, QueryParser
from kerui_recruit.search.service import HybridSearchService


class FakeParserClient:
    """只回一个固定 ParsedSearchPlan（或抛错），用于逐字段校验与回退链测试。"""

    model = "fake-parse"

    def __init__(self, plan: ParsedSearchPlan | None = None, error: Exception | None = None):
        self.plan = plan
        self.error = error
        self.calls = 0

    async def complete_json(self, messages, model, **kwargs):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.plan


def parse(text: str, plan: ParsedSearchPlan | None = None, error: Exception | None = None,
          parser: QueryParser | None = None):
    client = None if parser is not None else FakeParserClient(plan, error)
    engine = parser or QueryParser(client)
    return asyncio.run(engine.parse(text)), client


def test_parser_drops_entities_without_source_span() -> None:
    """公司/学校/姓名/职位必须能在原文定位：模型凭空补的一律丢弃。"""
    result, _ = parse("招聘 Java 后端，现居上海", ParsedSearchPlan(
        company="字节跳动", school="北京大学", name="张三", title="资深后端工程师",
        locations=["上海"], keywords=["Java"],
    ))

    assert result.filters.company is None
    assert result.filters.school is None
    assert result.filters.name is None
    assert result.filters.title is None
    assert result.filters.locations == ("上海",)
    assert result.keywords == "Java"
    assert result.source == "llm"


def test_parser_pushes_title_down_as_relaxable_hard_filter() -> None:
    """职位名下推为**可退化的**硬条件，**同时**并入词条做软排信号。

    索引侧 ``title`` 是整串子串匹配，而查询里的职位写法常是描述性长短语；抽检里
    11 条空结果有 8 条由该条件造成（单条可选择到 0 人）。所以不能只当硬条件，
    也不能只当词条：
    - 下推：职位名写准了（「全栈工程师」）理应真的筛，精度高于软排；
    - 并词条：`search/service.py:_relax_unmatchable_filters` 判定它「筛空」而退化时，
      条件从 where 里消失，词条仍在 FTS / 向量 / 重排里参与打分。
    """
    result, _ = parse("找全栈工程师，3 年以上", ParsedSearchPlan(
        title="全栈工程师", min_years=3, keywords=["全栈"],
    ))

    assert result.filters.title == "全栈工程师"
    assert result.filters.min_years == 3.0
    # 职位短语并入词条，退化后仍参与 FTS / 向量 / 重排打分。
    assert "全栈工程师" in result.keywords


def test_parser_pushes_company_down_as_relaxable_hard_filter_too() -> None:
    """公司名与职位名同一套处理：下推可退化硬条件 + 并入词条。"""
    result, _ = parse("找星展银行或汇丰控股的 Java 后端", ParsedSearchPlan(
        company="星展银行", companies=["汇丰控股"], keywords=["Java"],
    ))

    assert result.filters.company == "星展银行"
    assert result.filters.companies == ("汇丰控股",)
    # 多值公司也要逐个并入词条（不能只处理单值字段）。
    assert "星展银行" in result.keywords
    assert "汇丰控股" in result.keywords


def test_parser_rejects_out_of_range_years_and_keeps_rule_values() -> None:
    """区间越界（min > max）整组拒绝，规则解析值保留。"""
    result, _ = parse("3-5年 Java", ParsedSearchPlan(min_years=60.0, max_years=5.0))

    assert result.filters.min_years != 60.0
    assert result.filters.max_years in (None, 5.0)


def test_parser_drops_unknown_enums() -> None:
    """未知枚举报废：模型给的非法值一律不采纳，最终回落到规则链路。"""
    result, _ = parse("招个靠谱的人", ParsedSearchPlan(
        school_level="C9", gender="未知", school_region="galaxy",
    ))

    assert result.filters.school_level is None
    assert result.filters.gender is None
    assert result.filters.school_region is None
    assert result.source == "rule" and result.degraded == "rejected"


def test_parser_accepts_enums_with_source_cues() -> None:
    result, _ = parse("985 硕士 女生 海外背景", ParsedSearchPlan(
        school_level="985", gender="女", school_region="overseas",
    ))

    assert result.filters.school_level == "985"
    assert result.filters.gender == "女"
    assert result.filters.school_region == "overseas"


def test_parser_rejects_invalid_semantic_query_and_keeps_keywords() -> None:
    """语义查询违规（超长）→ 静默丢弃，词条照常使用。"""
    too_long = "Java" + "后端高并发分布式系统微服务架构设计经验" * 6
    result, _ = parse("Java 后端", ParsedSearchPlan(keywords=["Java"], semantic_query=too_long))

    assert result.semantic_query is None
    assert result.keywords == "Java"


def test_parser_keeps_valid_semantic_query() -> None:
    result, _ = parse("Java 后端", ParsedSearchPlan(keywords=["Java"], semantic_query="Java 后端"))

    assert result.semantic_query == "Java 后端"


def test_parser_falls_back_to_rules_on_timeout_and_error() -> None:
    timed_out, _ = parse("Java 后端", error=asyncio.TimeoutError())
    assert timed_out.source == "rule" and timed_out.degraded == "timeout"
    assert "Java" in timed_out.keywords

    broken, _ = parse("Java 后端", error=RuntimeError("boom"))
    assert broken.source == "rule" and broken.degraded == "provider_error"


def test_parser_without_client_is_pure_rule() -> None:
    result, _ = parse("上海 Java 3-5年", parser=QueryParser(None))

    assert result.source == "rule" and result.degraded is None
    assert result.filters.locations == ("上海",)
    assert result.filters.min_years == 3.0


def test_parser_caches_by_prompt_and_lexicon_version() -> None:
    plan = ParsedSearchPlan(keywords=["Java"])
    parser = QueryParser(FakeParserClient(plan))
    client = parser._client

    first = asyncio.run(parser.parse("Java 后端"))
    second = asyncio.run(parser.parse("Java 后端"))

    assert client.calls == 1
    assert first == second


def test_parser_accepts_direction_code_confirmed_by_lexicon() -> None:
    """C（分类器词表）印证命中的方向 code 直接采用。"""
    result, _ = parse("找做风控引擎的人", ParsedSearchPlan(
        business_directions=["RISK_CREDIT"], keywords=["风控引擎"],
    ))

    assert result.filters.business_directions == ("RISK_CREDIT",)


def test_parser_drops_hallucinated_direction_code() -> None:
    """C 无印证、也不是枚举标签 → 视为幻觉 code，不产生硬条件。"""
    result, _ = parse("找做风控引擎的人", ParsedSearchPlan(
        business_directions=["SALES_MANAGEMENT"], keywords=["风控引擎"],
    ))

    assert result.filters.business_directions == ()


def test_parser_accepts_direction_label_without_lexicon_hit() -> None:
    """A 有 C 无时，只有「原文里出现该枚举标准标签」才采用。"""
    result, _ = parse("找 AI 应用集成 的人", ParsedSearchPlan(
        career_specializations=["AI 应用集成"], keywords=["AI"],
    ))

    assert result.filters.career_specializations == ("BACKEND_AI_APPLICATION",)


def test_parser_drops_direction_code_without_text_evidence() -> None:
    """回归：拿 LLM 自己回的 code 当依据等于没校验。

    提示词要求模型回 code，所以 ``raw`` 天然等于 code；实测「AI 效能 全栈」这类
    没有方向词的查询会被解析出 OPS / ALGORITHM 一堆方向硬条件。原文无依据必须丢弃。
    """
    result, _ = parse("AI 效能 全栈", ParsedSearchPlan(
        career_directions=["OPS", "ALGORITHM"], keywords=["AI", "全栈"],
    ))

    assert result.filters.career_directions == ()


def test_parser_accepts_direction_code_when_label_is_in_the_query() -> None:
    """原文出现精确标签即算有依据（即使 C 侧词表反查不到）。"""
    result, _ = parse("需要全栈交付能力", ParsedSearchPlan(
        career_specializations=["BACKEND_FULL_STACK"], keywords=["全栈"],
    ))

    assert result.filters.career_specializations == ("BACKEND_FULL_STACK",)


def test_parser_reports_unparsed_terms() -> None:
    result, _ = parse("Java 后端 抗压能力", ParsedSearchPlan(keywords=["Java"]))

    assert any("抗压" in term for term in result.unparsed_terms)


class SpyEmbedding:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def embed_query(self, text):
        self.calls.append(text)
        return [1., 0.]


class SpyReranker:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def rerank(self, query, documents, **kwargs):
        self.calls.append(query)
        return []


@pytest.mark.asyncio
async def test_semantic_query_from_parser_reaches_vector_channel_without_rewriter(tmp_path: Path) -> None:
    """解析产出的语义查询直接进入向量通道：无需 rewriter，等于省掉一次独立改写调用。"""
    from kerui_recruit.search.contracts import SearchChunk
    from kerui_recruit.search.lancedb_index import LanceDBSearchIndex

    index = LanceDBSearchIndex(tmp_path / "index", vector_dimension=2)
    index.upsert([SearchChunk("chunk0", "c0", "r0", "Java", (1., 0.), 5, "MASTER", "上海", "AVAILABLE")])
    embedding, reranker = SpyEmbedding(), SpyReranker()
    service = HybridSearchService(index=index, embedding_provider=embedding, reranker_provider=reranker)

    await service.search("Java", CandidateFilters(), limit=20, mode="vector",
                         semantic_query="候选人画像：Java 后端")

    # 原查询向量 A 始终存在，解析出的语义查询 B 只作补充（两者都嵌入）。
    assert embedding.calls == ["Java", "候选人画像：Java 后端"]
