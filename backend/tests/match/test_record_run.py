from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from kerui_recruit.db.migrate import migrate
from kerui_recruit.db.models import Blob, Candidate, ResumeDocument, ResumeRevision, Jd, JdRevision, MatchResult, MatchRun
from kerui_recruit.db.session import create_engine_for
from kerui_recruit.match.service import MatchService, ReverseMatchRecord
from kerui_recruit.providers.local import (
    LocalHashEmbeddingProvider,
    LocalKeywordReranker,
)
from kerui_recruit.search.contracts import CandidateFilters, SearchHit
from kerui_recruit.search.service import HybridSearchService


class FakeIndex:
    def is_ready(self) -> bool:
        return True

    def filter_search(self, filters: CandidateFilters, limit: int) -> list[SearchHit]:
        return []

    def search(self, request) -> list[SearchHit]:
        return []


def _match_service(session_factory: sessionmaker[Session]) -> MatchService:
    return MatchService(
        session_factory=session_factory,
        search_service=HybridSearchService(
            index=FakeIndex(),
            embedding_provider=LocalHashEmbeddingProvider(dimension=64),
            reranker_provider=LocalKeywordReranker(),
        ),
    )


@pytest.fixture
def session_factory(tmp_path: Path) -> sessionmaker[Session]:
    engine = create_engine_for(tmp_path / "recruit.sqlite3")
    migrate(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    # Match results must refer to real, current, eligible SQLite entities.
    with factory.begin() as session:
        for number in (1, 2):
            session.add(Candidate(id=f"cand-{number}", display_name=f"Candidate {number}", status="AVAILABLE"))
            session.add(Blob(id=f"blob-{number}", content_sha256=str(number).zfill(64), suffix=".txt",
                             size_bytes=20, storage_path=f"blob-{number}"))
            session.flush()
            session.add(ResumeDocument(id=f"doc-{number}", candidate_id=f"cand-{number}"))
            session.flush()
            session.add(ResumeRevision(id=f"rev-{number}", document_id=f"doc-{number}", blob_id=f"blob-{number}",
                                       content_sha256=str(number).zfill(64), original_filename=f"resume-{number}.txt",
                                       status="READY", is_current=True, raw_text="Java Python"))
    return factory


def test_record_run_persists_snapshot(session_factory: sessionmaker[Session]) -> None:
    with session_factory() as session:
        jd = Jd(company="A", title="Java", status="OPEN")
        session.add(jd)
        session.commit()
        revision = JdRevision(
            jd_id=jd.id,
            source_text="Java 5年",
            min_years=Decimal("5.0"),
            status="READY",
            is_current=True,
            parsed_data={"summary": "Java 金融", "tech_direction": ["Java"]},
        )
        session.add(revision)
        session.commit()

        service = _match_service(session_factory)
        recorded = service.record_run(
            revision_id=revision.id,
            hits=[
                SearchHit(
                    chunk_id="c1",
                    candidate_id="cand-1",
                    revision_id="rev-1",
                    content="Java 支付",
                    score=0.9,
                    matched_channels=("bm25",),
                    total_years=6.0,
                    highest_degree="MASTER",
                    location="上海",
                )
            ],
        )

    with session_factory() as session:
        run = session.get(MatchRun, recorded.run_id)
        assert run is not None
        assert run.trigger == "JD_MATCH"
        result = session.scalars(select(MatchResult)).one()
        assert result.candidate_id == "cand-1"
        assert result.total_score is not None
        assert result.reason is not None
        assert "综合得分" in result.reason
        assert recorded.result_ids["cand-1"] == result.id


def test_record_reverse_run_persists_results(session_factory: sessionmaker[Session]) -> None:
    with session_factory() as session:
        jd = Jd(company="A", title="Java", status="OPEN")
        session.add(jd)
        session.commit()
        revision = JdRevision(
            jd_id=jd.id,
            source_text="Java 5年",
            min_years=Decimal("5.0"),
            status="READY",
            is_current=True,
            parsed_data={"summary": "Java 金融", "tech_direction": ["Java"]},
        )
        session.add(revision)
        session.commit()
        jd_id, revision_id = jd.id, revision.id

    service = _match_service(session_factory)
    record = ReverseMatchRecord(
        jd_id=jd_id,
        revision_id=revision_id,
        company="A",
        title="Java",
        hit=SearchHit(
            chunk_id="c1",
            candidate_id="cand-1",
            revision_id="rev-1",
            content="Java 支付",
            score=0.9,
            matched_channels=("bm25",),
            total_years=6.0,
            highest_degree="MASTER",
            location="上海",
        ),
    )
    recorded = service.record_reverse_run(candidate_id="cand-1", records=[record])

    with session_factory() as session:
        run = session.get(MatchRun, recorded.run_id)
        assert run is not None
        assert run.trigger == "REVERSE_MATCH"
        result = session.scalars(select(MatchResult)).one()
        assert result.candidate_id == "cand-1"
        assert result.jd_revision_id == revision_id
        assert result.resume_revision_id == "rev-1"
        assert result.status == "未处理"


def test_record_run_persists_mode(session_factory: sessionmaker[Session]) -> None:
    with session_factory() as session:
        jd = Jd(company="A", title="Java", status="OPEN")
        session.add(jd)
        session.commit()
        revision = JdRevision(jd_id=jd.id, source_text="Java", status="READY", is_current=True,
                              parsed_data={"summary": "Java"})
        session.add(revision)
        session.commit()
        revision_id = revision.id

    service = _match_service(session_factory)
    recorded = service.record_run(
        revision_id=revision_id,
        hits=[SearchHit(chunk_id="c1", candidate_id="cand-1", revision_id="rev-1",
                        content="Java", score=0.9, matched_channels=("bm25",),
                        total_years=6.0, highest_degree="MASTER", location="上海")],
        mode="vector",
    )
    with session_factory() as session:
        assert session.get(MatchRun, recorded.run_id).mode == "vector"


def test_record_reverse_run_persists_mode(session_factory: sessionmaker[Session]) -> None:
    with session_factory() as session:
        jd = Jd(company="A", title="Java", status="OPEN")
        session.add(jd)
        session.commit()
        revision = JdRevision(jd_id=jd.id, source_text="Java", status="READY", is_current=True,
                              parsed_data={"summary": "Java"})
        session.add(revision)
        session.commit()
        jd_id, revision_id = jd.id, revision.id

    service = _match_service(session_factory)
    record = ReverseMatchRecord(
        jd_id=jd_id, revision_id=revision_id, company="A", title="Java",
        hit=SearchHit(chunk_id="c1", candidate_id="cand-1", revision_id="rev-1",
                      content="Java", score=0.9, matched_channels=("bm25",),
                      total_years=6.0, highest_degree="MASTER", location="上海"),
    )
    recorded = service.record_reverse_run(candidate_id="cand-1", records=[record], mode="keyword")
    with session_factory() as session:
        assert session.get(MatchRun, recorded.run_id).mode == "keyword"