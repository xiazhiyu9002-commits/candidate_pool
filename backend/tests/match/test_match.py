from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from kerui_recruit.db.migrate import migrate
from kerui_recruit.db.models import Blob, Candidate, ResumeDocument, ResumeRevision, Jd, JdRevision
from kerui_recruit.db.session import create_engine_for
from kerui_recruit.match.service import MatchService
from kerui_recruit.providers.local import (
    LocalHashEmbeddingProvider,
    LocalKeywordReranker,
)
from kerui_recruit.search.contracts import (
    CandidateFilters,
    SearchChunk,
    SearchHit,
)
from kerui_recruit.search.service import HybridSearchService


class FakeIndex:
    """In-memory index that honors hard filters deterministically."""

    def __init__(self):
        self.chunks: list[SearchChunk] = []

    def upsert(self, chunks: list[SearchChunk]) -> None:
        self.chunks.extend(chunks)

    def delete_revision(self, revision_id: str) -> None:
        self.chunks = [c for c in self.chunks if c.revision_id != revision_id]

    def is_ready(self) -> bool:
        return len(self.chunks) > 0

    def filter_search(self, filters: CandidateFilters, limit: int) -> list[SearchHit]:
        return []

    def search(self, request) -> list[SearchHit]:
        degree_values = request.filters.degree_values()
        filtered = [
            c
            for c in self.chunks
            if (request.filters.min_years is None or (c.total_years or 0) >= request.filters.min_years)
            and (request.filters.max_years is None or (c.total_years or 0) <= request.filters.max_years)
            and (not degree_values or c.highest_degree in degree_values)
        ]
        return [
            SearchHit(
                chunk_id=c.id,
                candidate_id=c.candidate_id,
                revision_id=c.revision_id,
                content=c.content,
                score=1.0,
                matched_channels=("bm25",),
                total_years=c.total_years,
                highest_degree=c.highest_degree,
                location=c.location,
            )
            for c in filtered
        ]


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
                                       status="READY", is_current=True, raw_text="Java Python",
                                       parsed_data={"direction": "BACKEND", "skills": ["Python"]}))
    return factory


@pytest.mark.asyncio
async def test_match_excludes_candidates_below_min_years(session_factory: sessionmaker[Session]) -> None:
    index = FakeIndex()
    index.upsert(
        [
            SearchChunk(
                id="c1",
                candidate_id="cand-1",
                revision_id="rev-1",
                content="Python 金融",
                vector=[0.1],
                total_years=2.0,
                highest_degree="BACHELOR",
                location="北京",
                candidate_status="AVAILABLE",
            ),
            SearchChunk(
                id="c2",
                candidate_id="cand-2",
                revision_id="rev-2",
                content="Python 金融风控",
                vector=[0.1],
                total_years=6.0,
                highest_degree="MASTER",
                location="上海",
                candidate_status="AVAILABLE",
            ),
        ]
    )
    jd = Jd(company="A", title="Java", status="OPEN")
    with session_factory() as session:
        session.add(jd)
        session.commit()
    revision = JdRevision(
        jd_id=jd.id,
        source_text="Java 5年 硕士",
        min_years=Decimal("5.0"),
        highest_degree="MASTER",
        status="READY",
        is_current=True,
        parsed_data={"summary": "Java 金融风控", "tech_direction": ["Java"], "direction": "BACKEND"},
    )
    with session_factory() as session:
        session.add(revision)
        session.commit()

    service = MatchService(
        session_factory=session_factory,
        search_service=HybridSearchService(
            index=index,
            embedding_provider=LocalHashEmbeddingProvider(dimension=64),
            reranker_provider=LocalKeywordReranker(),
        ),
    )

    page = await service.match_jd(
        revision_id=revision.id,
        candidates=CandidateFilters(min_years=5.0, highest_degree="MASTER"),
        limit=20,
    )

    assert [hit.candidate_id for hit in page.items] == ["cand-2"]


@pytest.mark.asyncio
async def test_match_normalizes_chinese_degree_from_jd(session_factory: sessionmaker[Session]) -> None:
    index = FakeIndex()
    index.upsert(
        [
            SearchChunk(
                id="c1",
                candidate_id="cand-1",
                revision_id="rev-1",
                content="Java 支付",
                vector=[0.1],
                total_years=6.0,
                highest_degree="BACHELOR",
                location="上海",
                candidate_status="AVAILABLE",
            ),
        ]
    )
    jd = Jd(company="A", title="Java", status="OPEN")
    with session_factory() as session:
        session.add(jd)
        session.commit()
    revision = JdRevision(
        jd_id=jd.id,
        source_text="Java 3年 本科",
        min_years=Decimal("3.0"),
        highest_degree="本科",
        status="READY",
        is_current=True,
        parsed_data={"summary": "Java 后端", "tech_direction": ["Java"], "direction": "BACKEND"},
    )
    with session_factory() as session:
        session.add(revision)
        session.commit()

    service = MatchService(
        session_factory=session_factory,
        search_service=HybridSearchService(
            index=index,
            embedding_provider=LocalHashEmbeddingProvider(dimension=64),
            reranker_provider=LocalKeywordReranker(),
        ),
    )

    page = await service.match_jd(revision_id=revision.id, limit=20)

    assert [hit.candidate_id for hit in page.items] == ["cand-1"]


def test_duty_evidence_ranks_above_skill_only():
    service = MatchService(session_factory=None, search_service=None)

    class _Rev:
        id = "rev-1"
        jd_id = "jd-1"
        source_text = None
        min_years = None
        highest_degree = None
        location = None
        parsed_data = {"direction": "BACKEND", "required_skills": ["Java"],
                       "core_duties": ["负责支付高并发服务"]}

    context = MatchService._context(_Rev())
    hit = SearchHit(
        chunk_id="c1", candidate_id="cand-1", revision_id="r1", content="Java",
        score=0.0, matched_channels=(), total_years=5.0, highest_degree="MASTER", location="上海",
    )

    with_evidence = service._score_context(
        context, hit,
        {"direction": "BACKEND", "skills": ["Java"], "projects": [{"summary": "搭建支付高并发服务"}]},
    )
    without_evidence = service._score_context(
        context, hit,
        {"direction": "BACKEND", "skills": ["Java"]},
    )

    # 技能覆盖、方向相同：有真实职责证据者应优先于仅列技能名者。
    assert with_evidence.total > without_evidence.total


def test_plus_preference_lifts_ranking_without_rejecting():
    """优先项命中率进入总分：命中者排在未命中者前，但两者都不被淘汰。

    这是 skill / other_keyword 被降级为 PLUS 之后「精度靠排序补回来」的唯一机制。
    """
    service = MatchService(session_factory=None, search_service=None)

    class _Rev:
        id = "rev-1"
        jd_id = "jd-1"
        source_text = None
        min_years = None
        highest_degree = None
        location = None
        parsed_data = {
            "direction": "BACKEND",
            "required_skills": ["Java"],
            "exact_constraints": [
                {"kind": "skill", "operator": "OR", "alternatives": ["LangGraph"],
                 "strength": "PLUS", "source": "inferred", "source_text": "LangGraph 优先"},
            ],
        }

    context = MatchService._context(_Rev())
    hit = SearchHit(
        chunk_id="c1", candidate_id="cand-1", revision_id="r1", content="Java",
        score=0.0, matched_channels=(), total_years=5.0, highest_degree="MASTER", location="上海",
    )

    matched = service._score_context(
        context, hit, {"direction": "BACKEND", "skills": ["Java", "LangGraph"]}
    )
    missed = service._score_context(context, hit, {"direction": "BACKEND", "skills": ["Java"]})

    assert matched.total > missed.total
    assert matched.breakdown["preference"] == 1.0
    assert missed.breakdown["preference"] == 0.0


@pytest.mark.asyncio
async def test_match_keeps_unconfirmed_direction_candidate(session_factory: sessionmaker[Session]) -> None:
    index = FakeIndex()
    index.upsert(
        [
            SearchChunk("c1", "cand-1", "rev-1", "Python 金融", [0.1], 6.0, "MASTER", "上海", "AVAILABLE"),
            SearchChunk("c2", "cand-2", "rev-2", "Python 金融风控", [0.1], 6.0, "MASTER", "上海", "AVAILABLE"),
        ]
    )
    jd = Jd(company="A", title="Java", status="OPEN")
    with session_factory() as session:
        session.add(jd)
        session.commit()
    revision = JdRevision(
        jd_id=jd.id, source_text="Java 金融风控", min_years=Decimal("5.0"),
        highest_degree="MASTER", status="READY", is_current=True,
        parsed_data={"summary": "Java 金融风控", "direction": "BACKEND"},
    )
    with session_factory() as session:
        session.add(revision)
        session.commit()
        # cand-1 方向置空（未确认），cand-2 保持 BACKEND。
        resume = session.get(ResumeRevision, "rev-1")
        parsed = dict(resume.parsed_data or {})
        parsed["direction"] = None
        resume.parsed_data = parsed
        session.commit()

    service = MatchService(
        session_factory=session_factory,
        search_service=HybridSearchService(
            index=index,
            embedding_provider=LocalHashEmbeddingProvider(dimension=64),
            reranker_provider=LocalKeywordReranker(),
        ),
    )
    page = await service.match_jd(revision_id=revision.id, limit=20)

    # 方向未确认的候选人不再被硬过滤，仍正常返回。
    assert {hit.candidate_id for hit in page.items} == {"cand-1", "cand-2"}
