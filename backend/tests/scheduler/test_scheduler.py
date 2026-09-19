from decimal import Decimal
import asyncio
import threading
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from kerui_recruit.db.migrate import migrate
from kerui_recruit.db.models import Blob, Candidate, Jd, JdRevision, ResumeDocument, ResumeRevision
from kerui_recruit.match.jd_index import JdSearchIndex
from kerui_recruit.db.session import create_engine_for
from kerui_recruit.match.service import MatchService
from kerui_recruit.providers.local import LocalHashEmbeddingProvider, LocalKeywordReranker
from kerui_recruit.scheduler.service import SchedulerService
from kerui_recruit.search.contracts import CandidateFilters, SearchChunk, SearchHit
from kerui_recruit.search.service import HybridSearchService


class FakeIndex:
    def __init__(self, chunks: list[SearchChunk]) -> None:
        self.chunks = chunks

    def upsert(self, chunks: list[SearchChunk]) -> None:
        self.chunks.extend(chunks)

    def delete_revision(self, revision_id: str) -> None:
        self.chunks = [c for c in self.chunks if c.revision_id != revision_id]

    def is_ready(self) -> bool:
        return len(self.chunks) > 0

    def filter_search(self, filters: CandidateFilters, limit: int) -> list[SearchHit]:
        return []

    def search(self, request) -> list[SearchHit]:
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
            for c in self.chunks
        ]


@pytest.fixture
def session_factory(tmp_path: Path) -> sessionmaker[Session]:
    engine = create_engine_for(tmp_path / "recruit.sqlite3")
    migrate(engine)
    return sessionmaker(engine, expire_on_commit=False)


@pytest.mark.asyncio
async def test_reverse_match_candidate_finds_open_jds(session_factory: sessionmaker[Session], tmp_path: Path) -> None:
    with session_factory() as session:
        candidate = Candidate(display_name="张三", status="AVAILABLE")
        jd = Jd(company="某公司", title="Java", status="OPEN")
        session.add_all([candidate, jd])
        session.commit()
        revision = JdRevision(
            jd_id=jd.id,
            source_text="Java 3年",
            status="READY",
            is_current=True,
            parsed_data={
                "summary": "Java 后端",
                "required_skills": ["Java"],
                "direction": "BACKEND",
            },
        )
        session.add(revision)
        session.commit()
        candidate_id, revision_id = candidate.id, revision.id
        blob = Blob(content_sha256="b" * 64, suffix=".txt", size_bytes=30, storage_path="test-resume")
        document = ResumeDocument(candidate_id=candidate_id)
        session.add_all([blob, document])
        session.flush()
        session.add(ResumeRevision(id="rev-1", document_id=document.id, blob_id=blob.id,
                                   content_sha256=blob.content_sha256, original_filename="resume.txt",
                                   status="READY", is_current=True, raw_text="Java 后端工程师",
                                   parsed_data={
                                       "total_years": 4, "highest_degree": "BACHELOR", "location": "上海",
                                       "skills": ["Java"], "direction": "BACKEND",
                                   }))
        session.commit()

    index = FakeIndex(
        [
            SearchChunk(
                id="c1",
                candidate_id=candidate_id,
                revision_id="rev-1",
                content="Java 后端工程师",
                vector=[0.1],
                total_years=4.0,
                highest_degree="BACHELOR",
                location="上海",
                candidate_status="AVAILABLE",
                direction="BACKEND",
            )
        ]
    )
    embedding = LocalHashEmbeddingProvider(dimension=64)
    jd_index = JdSearchIndex(tmp_path / "jobs", vector_dimension=64)
    jd_index.upsert(jd_id=jd.id, revision_id=revision_id, content="Java 后端",
                    vector=(await embedding.embed_documents(["Java 后端"]))[0], direction="BACKEND")
    match_service = MatchService(
        session_factory=session_factory,
        jd_index=jd_index,
        search_service=HybridSearchService(
            index=index,
            embedding_provider=embedding,
            reranker_provider=LocalKeywordReranker(),
        ),
    )
    scheduler = SchedulerService(
        session_factory=session_factory,
        match_service=match_service,
        reminder_service=None,
    )

    matches = await scheduler.reverse_match_candidate(candidate_id)

    assert len(matches) == 1
    assert matches[0].jd_id == jd.id
    assert matches[0].company == "某公司"
    assert matches[0].title == "Java"
    assert matches[0].score == 1.0


@pytest.mark.asyncio
async def test_scheduler_blocking_integrations_do_not_stall_event_loop(session_factory):
    import time
    release = threading.Event()
    started = threading.Event()

    class BlockingMail:
        def poll_and_ingest(self, sender_domains=None, resume_gate=None):
            started.set()
            release.wait(1)
            return []

    scheduler = SchedulerService(session_factory=session_factory, match_service=None,
                                 reminder_service=None, mail_ingest_service=BlockingMail(),
                                 mail_auto_sync=True)
    timer = threading.Timer(0.3, release.set)
    timer.start()
    started_at = time.monotonic()
    running = asyncio.create_task(scheduler.run_forever(interval_seconds=60))
    assert await asyncio.to_thread(started.wait, 0.5)
    await asyncio.sleep(0.02)
    assert time.monotonic() - started_at < 0.2
    release.set()
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    timer.cancel()
