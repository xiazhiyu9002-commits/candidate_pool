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
    assert "done" in stages
    assert all("message" in event for event in events)
