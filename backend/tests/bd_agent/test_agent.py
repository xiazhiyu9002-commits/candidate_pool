from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from kerui_recruit.bd_agent.agent import BdAgent
from kerui_recruit.bd_agent.evidence import EvidenceExtractor
from kerui_recruit.bd_search.service import BdSearchService, WebSearchProvider, WebSearchResult
from kerui_recruit.bd_agent.synthesis import (
    EvidenceItem,
    SynthesisResult,
    SynthesizedLead,
)
from kerui_recruit.db.migrate import migrate
from kerui_recruit.db.models import BdLead, BdSearchSession
from kerui_recruit.db.session import create_engine_for
from kerui_recruit.encryption.service import EncryptionService


class FakeSearch(WebSearchProvider):
    def search(self, query: str, limit: int = 10) -> list[WebSearchResult]:
        return [
            WebSearchResult(
                title="A公司招聘",
                url="https://a.com/job",
                snippet="snip",
                source="web",
                raw_content="A公司招聘大模型工程师",
            )
        ]


class FakeFetcher:
    async def fetch(self, url: str) -> str | None:
        return "fetched content"


class FakePlanner:
    async def plan(self, query: str, max_queries: int = 3) -> list[str]:
        return ["q1", "q2"]


class FakeSynthesizer:
    def __init__(self, result: SynthesisResult) -> None:
        self._result = result

    async def synthesize(self, query, chunks):
        return self._result


@pytest.fixture
def session_factory(tmp_path: Path) -> sessionmaker[Session]:
    engine = create_engine_for(tmp_path / "recruit.sqlite3")
    migrate(engine)
    return sessionmaker(engine, expire_on_commit=False)


def _result() -> SynthesisResult:
    return SynthesisResult(
        leads=[
            SynthesizedLead(
                company="A公司",
                job_title="大模型工程师",
                is_hiring=True,
                confidence=0.9,
                evidence=[EvidenceItem(claim="在招", source_url="https://a.com/job")],
            )
        ]
    )


def make_agent(
    session_factory: sessionmaker[Session],
    tmp_path: Path,
    **kwargs,
) -> BdAgent:
    encryption = EncryptionService(key_path=str(tmp_path / "key"))
    return BdAgent(
        session_factory=session_factory,
        search_provider=FakeSearch(),
        fetcher=FakeFetcher(),
        encryption=encryption,
        planner=kwargs.get("planner", FakePlanner()),
        evidence_extractor=EvidenceExtractor(),
        synthesizer=kwargs.get("synthesizer", FakeSynthesizer(_result())),
    )


@pytest.mark.asyncio
async def test_run_persists_session_and_encrypted_leads(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    agent = make_agent(session_factory, tmp_path)
    result = await agent.run("找大模型公司")

    assert result.session_id
    assert len(result.leads) == 1
    assert result.leads[0].is_hiring is True
    assert agent.encryption.decrypt(result.leads[0].company_name) == "A公司"

    with session_factory() as session:
        saved = session.scalars(
            select(BdSearchSession).where(BdSearchSession.id == result.session_id)
        ).one()
        assert saved.query == "找大模型公司"
        assert len(saved.leads) == 1


@pytest.mark.asyncio
async def test_run_without_llm_returns_empty(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    encryption = EncryptionService(key_path=str(tmp_path / "key"))
    agent = BdAgent(
        session_factory=session_factory,
        search_provider=FakeSearch(),
        fetcher=FakeFetcher(),
        encryption=encryption,
        planner=None,
        synthesizer=None,
    )
    result = await agent.run("q")
    assert result.session_id == ""
    assert result.leads == []


@pytest.mark.asyncio
async def test_run_without_llm_uses_fallback(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    encryption = EncryptionService(key_path=str(tmp_path / "key"))
    fallback = BdSearchService(
        session_factory=session_factory,
        search_provider=FakeSearch(),
        encryption=encryption,
    )
    agent = BdAgent(
        session_factory=session_factory,
        search_provider=FakeSearch(),
        fetcher=FakeFetcher(),
        encryption=encryption,
        planner=None,
        synthesizer=None,
        fallback=fallback,
    )
    result = await agent.run("q")
    assert result.session_id == ""
    assert len(result.leads) == 1


@pytest.mark.asyncio
async def test_run_emits_progress_events(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    agent = make_agent(session_factory, tmp_path)
    queue: asyncio.Queue = asyncio.Queue()
    await agent.run("找大模型公司", progress=queue)

    events: list[dict] = []
    while not queue.empty():
        events.append(queue.get_nowait())

    stages = [event["stage"] for event in events]
    assert stages[0] == "planning"
    assert "planned" in stages
    assert "searching" in stages
    # 重排与综合是「重排之后就没数据」那类问题的关键环节，必须显式断言它们真的发生过。
    assert "fetched" in stages
    assert "ranking" in stages
    assert "synthesizing" in stages
    assert "synthesized" in stages
    assert "leads" in stages
    assert "done" in stages
    assert all("message" in event for event in events)


def test_synthesis_budget_is_not_clipped_by_total_deadline() -> None:
    """综合上限必须小于总预算。

    综合的阶段超时是 ``min(_SYNTHESIS_TIMEOUT_SECONDS, 剩余总预算)``。若总预算更小，
    思考模型永远拿不到完整预算——实测 60 秒上限 + 120 秒总预算把**每一次** BD 检索
    都掐断，表现为「重排之后就结束、没有数据」。这条断言防止有人再把预算调反。
    """
    from kerui_recruit.bd_agent import agent as agent_module

    assert agent_module._SYNTHESIS_TIMEOUT_SECONDS < agent_module._DEFAULT_DEADLINE_SECONDS


@pytest.mark.asyncio
async def test_slow_synthesis_within_budget_is_not_treated_as_degraded(
    session_factory: sessionmaker[Session], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """综合在预算内慢一点也属于正常完成，不得被判成降级。"""
    from kerui_recruit.bd_agent import agent as agent_module

    monkeypatch.setattr(agent_module, "_SYNTHESIS_TIMEOUT_SECONDS", 2.0)
    monkeypatch.setattr(agent_module, "_DEFAULT_DEADLINE_SECONDS", 10.0)

    class SlowButOk:
        async def synthesize(self, query, chunks):
            await asyncio.sleep(0.3)
            return _result()

    agent = BdAgent(
        session_factory=session_factory,
        search_provider=FakeSearch(),
        fetcher=FakeFetcher(),
        encryption=EncryptionService(key_path=str(tmp_path / "key")),
        planner=FakePlanner(),
        evidence_extractor=EvidenceExtractor(),
        synthesizer=SlowButOk(),
    )
    result = await agent.run("找大模型公司")

    assert result.degraded_reason is None
    assert len(result.leads) == 1


@pytest.mark.asyncio
async def test_run_emits_leads_per_round(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """每轮综合完就把该轮线索推给调用方，不必等整批结束。"""
    agent = make_agent(session_factory, tmp_path)
    queue: asyncio.Queue = asyncio.Queue()
    await agent.run("找大模型公司", progress=queue)

    events: list[dict] = []
    while not queue.empty():
        events.append(queue.get_nowait())

    lead_events = [event for event in events if event["stage"] == "leads"]
    assert len(lead_events) == 1
    assert len(lead_events[0]["leads"]) == 1
    assert lead_events[0]["message"]


def test_recover_company_name_from_title() -> None:
    """标题里的公司名优先：招聘站标题常形如「XX科技招聘算法工程师-北京」。"""
    from kerui_recruit.bd_agent.agent import _recover_company_name

    assert (
        _recover_company_name(
            title="XX科技招聘算法工程师-北京", url="https://www.zhipin.com/job/1", snippet=None
        )
        == "XX科技"
    )
    assert (
        _recover_company_name(
            title="【阿里巴巴】高级算法工程师", url="https://www.liepin.com/job/1", snippet=None
        )
        == "阿里巴巴"
    )


def test_recover_company_name_falls_back_to_snippet_then_domain() -> None:
    from kerui_recruit.bd_agent.agent import _recover_company_name

    # 标题只有岗位名 → 退回摘要里带公司后缀的名字
    assert (
        _recover_company_name(
            title="算法工程师-BOSS直聘",
            url="https://www.zhipin.com/job/1",
            snippet="上海某某科技有限公司诚招算法工程师",
        )
        == "上海某某科技有限公司"
    )
    # 标题与摘要都没有 → 退回公司官网招聘页域名
    assert (
        _recover_company_name(
            title="算法工程师", url="https://jobs.bytedance.com/careers/1", snippet="职责描述"
        )
        == "bytedance"
    )


def test_recover_company_name_rejects_placeholder_and_job_board_domain() -> None:
    """泛称不算公司名，招聘平台的域名也不含雇主信息——宁可返回 None。"""
    from kerui_recruit.bd_agent.agent import _recover_company_name

    assert _recover_company_name(title="我们公司招聘", url=None, snippet=None) is None
    assert _recover_company_name(title="大公司直招", url=None, snippet=None) is None
    assert _recover_company_name(title="算法工程师", url="https://www.zhipin.com/job/1", snippet=None) is None
    # 普通站点的非招聘页不做域名兜底，避免把 «example» 当成公司
    assert _recover_company_name(title=None, url="https://www.example.com/blog/1", snippet=None) is None


@pytest.mark.asyncio
async def test_persist_recovers_company_from_page_title_when_model_returns_none(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """模型没给公司名时，落库前用页面标题回捞，而不是直接写「未识别公司」。"""
    unnamed = SynthesisResult(
        leads=[
            SynthesizedLead(
                company=None,
                job_title="大模型工程师",
                confidence=0.5,
                evidence=[EvidenceItem(claim="在招", source_url="https://a.com/job")],
            )
        ]
    )
    agent = make_agent(session_factory, tmp_path, synthesizer=FakeSynthesizer(unnamed))
    result = await agent.run("找大模型公司")

    assert len(result.leads) == 1
    # FakeSearch 的标题是「A公司招聘」→ 回捞到的公司名是「A公司」，不是「未识别公司」
    assert agent.encryption.decrypt(result.leads[0].company_name) == "A公司"


@pytest.mark.asyncio
async def test_persist_keeps_placeholder_when_nothing_to_recover(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """标题、摘要、域名都捞不到时才退回「未识别公司」（兜底不被移除）。"""

    class NoHintSearch(WebSearchProvider):
        def search(self, query: str, limit: int = 10) -> list[WebSearchResult]:
            return [
                WebSearchResult(
                    title="算法工程师",
                    url="https://www.zhipin.com/job/1",
                    snippet="岗位职责描述",
                    source="web",
                )
            ]

    unnamed = SynthesisResult(
        leads=[
            SynthesizedLead(
                company=None,
                job_title="大模型工程师",
                confidence=0.5,
                evidence=[EvidenceItem(claim="在招", source_url="https://www.zhipin.com/job/1")],
            )
        ]
    )
    agent = BdAgent(
        session_factory=session_factory,
        search_provider=NoHintSearch(),
        fetcher=FakeFetcher(),
        encryption=EncryptionService(key_path=str(tmp_path / "key")),
        planner=FakePlanner(),
        evidence_extractor=EvidenceExtractor(),
        synthesizer=FakeSynthesizer(unnamed),
    )
    result = await agent.run("找大模型公司")

    assert agent.encryption.decrypt(result.leads[0].company_name) == "未识别公司"


class CountingFetcher:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def fetch(self, url: str) -> str | None:
        self.calls.append(url)
        return "fetched content"


class ManyResultsSearch(WebSearchProvider):
    """9 条普通来源 + 1 条招聘平台来源（来源质量最高，应优先被抓取）。"""

    def search(self, query: str, limit: int = 10) -> list[WebSearchResult]:
        results = [
            WebSearchResult(
                title=f"公司{index}招聘",
                url=f"https://company{index}.example/job",
                snippet=f"摘要{index}",
                source="web",
            )
            for index in range(9)
        ]
        results.append(
            WebSearchResult(
                title="LinkedIn 岗位",
                url="https://www.linkedin.com/jobs/view/1",
                snippet="招聘摘要",
                source="web",
            )
        )
        return results


@pytest.mark.asyncio
async def test_search_fetches_only_top_quality_pages(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """只抓来源质量最高的前 K 个页面正文，其余复用搜索摘要。"""
    fetcher = CountingFetcher()
    agent = BdAgent(
        session_factory=session_factory,
        search_provider=ManyResultsSearch(),
        fetcher=fetcher,  # type: ignore[arg-type]
        encryption=EncryptionService(key_path=str(tmp_path / "key")),
        planner=FakePlanner(),
        evidence_extractor=EvidenceExtractor(),
        synthesizer=FakeSynthesizer(SynthesisResult()),
        fetch_top_k=2,
    )

    await agent.run("找公司")

    assert set(fetcher.calls) == {
        "https://www.linkedin.com/jobs/view/1",
        "https://company0.example/job",
    }


class SlowSynthesizer:
    """第一次正常返回、之后挂起，用于验证单阶段超时不会拖住整轮。"""

    def __init__(self, result: SynthesisResult, slow_from: int = 1) -> None:
        self._result = result
        self._slow_from = slow_from
        self.calls = 0

    async def synthesize(self, query, chunks):
        self.calls += 1
        if self.calls > self._slow_from:
            await asyncio.sleep(30)
        return self._result


@pytest.mark.asyncio
async def test_slow_synthesis_is_cut_and_accumulated_leads_survive(
    session_factory: sessionmaker[Session], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kerui_recruit.bd_agent import agent as agent_module

    monkeypatch.setattr(agent_module, "_SYNTHESIS_TIMEOUT_SECONDS", 0.05)

    synthesizer = SlowSynthesizer(
        SynthesisResult(
            leads=[
                SynthesizedLead(
                    company="A公司",
                    job_title="大模型工程师",
                    is_hiring=True,
                    confidence=0.9,
                    evidence=[EvidenceItem(claim="在招", source_url="https://a.com/job")],
                )
            ],
            follow_up_queries=["q3"],  # 触发第二轮
        ),
        slow_from=1,
    )
    agent = BdAgent(
        session_factory=session_factory,
        search_provider=FakeSearch(),
        fetcher=FakeFetcher(),
        encryption=EncryptionService(key_path=str(tmp_path / "key")),
        planner=FakePlanner(),
        evidence_extractor=EvidenceExtractor(),
        synthesizer=synthesizer,
    )

    result = await agent.run("找大模型公司")

    assert synthesizer.calls == 2  # 第二轮被超时掐断，没有继续烧预算
    assert len(result.leads) == 1  # 已攒到的线索照常返回
    # 超时属于降级，必须留下原因，否则前端只能显示「暂无线索」，把失败伪装成空结果。
    assert result.degraded_reason
    assert "综合" in result.degraded_reason


class BoomSynthesizer:
    def __init__(self, error: BaseException) -> None:
        self._error = error

    async def synthesize(self, query, chunks):
        raise self._error


def _events(queue: asyncio.Queue) -> list[dict]:
    events: list[dict] = []
    while not queue.empty():
        events.append(queue.get_nowait())
    return events


@pytest.mark.asyncio
async def test_provider_failure_is_surfaced_not_swallowed(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """模型不可用必须暴露成降级原因，而不是伪装成「没搜到线索」。"""
    from kerui_recruit.providers.errors import ProviderError

    agent = BdAgent(
        session_factory=session_factory,
        search_provider=FakeSearch(),
        fetcher=FakeFetcher(),
        encryption=EncryptionService(key_path=str(tmp_path / "key")),
        planner=FakePlanner(),
        evidence_extractor=EvidenceExtractor(),
        synthesizer=BoomSynthesizer(
            ProviderError(
                code="E_AI_NO_PROVIDER",
                retryable=False,
                user_message="没有可用的 AI 供应商",
            )
        ),
    )
    queue: asyncio.Queue = asyncio.Queue()
    result = await agent.run("找大模型公司", progress=queue)

    assert result.leads == []
    assert result.degraded_reason is not None
    assert "没有可用的 AI 供应商" in result.degraded_reason
    assert "E_AI_NO_PROVIDER" in result.degraded_reason

    stages = [event["stage"] for event in _events(queue)]
    assert "ranking" in stages
    assert "synthesis_failed" in stages
    assert "done" in stages


@pytest.mark.asyncio
async def test_synthesis_failure_keeps_already_accumulated_leads(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """第一轮成功、第二轮失败：已入库的线索照常返回，同时带上降级原因。"""

    class FlakySynthesizer:
        def __init__(self) -> None:
            self.calls = 0

        async def synthesize(self, query, chunks):
            self.calls += 1
            if self.calls > 1:
                raise RuntimeError("boom")
            return _result_with_follow_up()

    agent = BdAgent(
        session_factory=session_factory,
        search_provider=FakeSearch(),
        fetcher=FakeFetcher(),
        encryption=EncryptionService(key_path=str(tmp_path / "key")),
        planner=FakePlanner(),
        evidence_extractor=EvidenceExtractor(),
        synthesizer=FlakySynthesizer(),
    )

    result = await agent.run("找大模型公司")

    assert len(result.leads) == 1
    assert result.degraded_reason
    assert "RuntimeError" in result.degraded_reason


def _result_with_follow_up() -> SynthesisResult:
    return SynthesisResult(
        leads=[
            SynthesizedLead(
                company="A公司",
                job_title="大模型工程师",
                is_hiring=True,
                confidence=0.9,
                evidence=[EvidenceItem(claim="在招", source_url="https://a.com/job")],
            )
        ],
        follow_up_queries=["q3"],
    )


@pytest.mark.asyncio
async def test_run_without_llm_reports_degradation(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """无 LLM 的降级也要留下原因，否则用户无从判断为什么总是没有线索。"""
    agent = BdAgent(
        session_factory=session_factory,
        search_provider=FakeSearch(),
        fetcher=FakeFetcher(),
        encryption=EncryptionService(key_path=str(tmp_path / "key")),
        planner=None,
        synthesizer=None,
    )
    result = await agent.run("q")
    assert result.degraded_reason is not None
    assert "基础搜索" in result.degraded_reason
