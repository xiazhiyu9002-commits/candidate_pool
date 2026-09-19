from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from kerui_recruit.db.migrate import migrate
from kerui_recruit.db.models import BdEvidence, BdLead, BdSearchSession
from kerui_recruit.db.session import create_engine_for


def make_session(database: Path) -> Session:
    engine = create_engine_for(database)
    migrate(engine)
    return Session(engine)


def test_bd_lead_extended_fields_persist(tmp_path: Path) -> None:
    with make_session(tmp_path / "recruit.sqlite3") as session:
        lead = BdLead(
            source="tavily",
            company_name="字节跳动",
            confidence=0.9,
            is_hiring=True,
            synthesized_json={"salary": "50-80K"},
        )
        session.add(lead)
        session.commit()

        loaded = session.scalars(select(BdLead)).one()
        assert loaded.confidence == 0.9
        assert loaded.is_hiring is True
        assert loaded.synthesized_json == {"salary": "50-80K"}


def test_bd_search_session_links_leads(tmp_path: Path) -> None:
    with make_session(tmp_path / "recruit.sqlite3") as session:
        search_session = BdSearchSession(query="找大模型算法公司", kind="text")
        session.add(search_session)
        session.flush()
        lead = BdLead(source="tavily", company_name="A公司", session_id=search_session.id)
        session.add(lead)
        session.commit()

        loaded = session.scalars(select(BdSearchSession)).one()
        assert loaded.query == "找大模型算法公司"
        assert len(loaded.leads) == 1
        assert loaded.leads[0].company_name == "A公司"


def test_bd_evidence_links_to_lead(tmp_path: Path) -> None:
    with make_session(tmp_path / "recruit.sqlite3") as session:
        lead = BdLead(source="tavily", company_name="B公司")
        session.add(lead)
        session.flush()
        evidence = BdEvidence(
            lead_id=lead.id,
            claim="正在招聘",
            quote="岗位职责：负责…",
            source_url="https://example.com/job",
            relevance_score=0.85,
        )
        session.add(evidence)
        session.commit()

        loaded = session.scalars(select(BdLead)).one()
        assert len(loaded.evidence) == 1
        assert loaded.evidence[0].claim == "正在招聘"
        assert loaded.evidence[0].source_url == "https://example.com/job"
