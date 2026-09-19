from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy.orm import Session, sessionmaker

from kerui_recruit.api.services import AppServices
from kerui_recruit.bd_agent.agent import BdAgent
from kerui_recruit.bd_agent.evidence import EvidenceExtractor
from kerui_recruit.bd_agent.synthesis import (
    EvidenceItem,
    SynthesisResult,
    SynthesizedLead,
)
from kerui_recruit.bd_search.service import WebSearchProvider, WebSearchResult
from kerui_recruit.core.settings import Settings
from kerui_recruit.db.migrate import migrate
from kerui_recruit.db.session import create_engine_for
from kerui_recruit.encryption.service import EncryptionService
from kerui_recruit.main import create_app


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
        return "fetched"


class FakePlanner:
    async def plan(self, query: str, max_queries: int = 3) -> list[str]:
        return ["q1"]


class FakeSynthesizer:
    async def synthesize(self, query, chunks):
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


@pytest.fixture
def client(tmp_path: Path):
    engine = create_engine_for(tmp_path / "recruit.sqlite3")
    migrate(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    encryption = EncryptionService(key_path=str(tmp_path / "key"))
    agent = BdAgent(
        session_factory=factory,
        search_provider=FakeSearch(),
        fetcher=FakeFetcher(),
        encryption=encryption,
        planner=FakePlanner(),
        evidence_extractor=EvidenceExtractor(),
        synthesizer=FakeSynthesizer(),
    )
    settings = Settings(data_root=tmp_path / "data", session_token=SecretStr("token"))
    services = AppServices(
        settings=settings,
        session_factory=factory,
        blob_store=None,  # type: ignore[arg-type]
        task_repository=None,  # type: ignore[arg-type]
        search_service=None,  # type: ignore[arg-type]
        bd_agent=agent,
        encryption_service=encryption,
    )
    app = create_app(services)
    with TestClient(app) as test_client:
        yield test_client


def _headers() -> dict[str, str]:
    return {"X-Kerui-Session": "token"}


def test_agent_query_returns_cited_leads(client: TestClient) -> None:
    resp = client.post(
        "/api/bd/agent/query",
        json={"query": "找大模型公司", "kind": "text"},
        headers=_headers(),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["session_id"]
    assert len(body["leads"]) == 1
    lead = body["leads"][0]
    assert lead["company_name"] == "A公司"
    assert lead["is_hiring"] is True
    assert lead["evidence"][0]["source_url"] == "https://a.com/job"


def test_agent_follow_up_reuses_session(client: TestClient) -> None:
    first = client.post(
        "/api/bd/agent/query",
        json={"query": "找大模型公司"},
        headers=_headers(),
    ).json()
    session_id = first["session_id"]

    second = client.post(
        f"/api/bd/agent/session/{session_id}/follow-up",
        json={"query": "补充搜索上海"},
        headers=_headers(),
    )
    assert second.status_code == 200
    assert second.json()["session_id"] == session_id


def test_agent_export_report(client: TestClient) -> None:
    created = client.post(
        "/api/bd/agent/query",
        json={"query": "找大模型公司"},
        headers=_headers(),
    ).json()
    session_id = created["session_id"]

    export = client.get(
        f"/api/bd/agent/session/{session_id}/export",
        headers=_headers(),
    )
    assert export.status_code == 200
    assert "text/markdown" in export.headers["content-type"]
    assert "A公司" in export.text


def test_agent_stream_returns_progress_and_result(client: TestClient) -> None:
    resp = client.post(
        "/api/bd/agent/query-stream",
        json={"query": "找大模型公司"},
        headers=_headers(),
    )
    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers["content-type"]
    body = resp.text
    assert "event: progress" in body
    assert "event: result" in body
    assert '"session_id"' in body
    assert "A公司" in body
