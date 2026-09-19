from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from kerui_recruit.db.migrate import migrate
from kerui_recruit.db.models import Blob, Candidate, IndexSyncRecord, ResumeDocument, ResumeRevision
from kerui_recruit.db.session import create_engine_for
from kerui_recruit.search.contracts import CandidateFilters
from kerui_recruit.search.lancedb_index import LanceDBSearchIndex
from kerui_recruit.search.sync import IndexSyncService, enqueue_sync
from kerui_recruit.soft_delete.service import SoftDeleteService


class Embedding:
    def __init__(self):
        self.fail = False
        self.before_return = None

    async def embed_documents(self, texts):
        if self.fail:
            raise RuntimeError("controlled provider outage")
        if self.before_return:
            callback, self.before_return = self.before_return, None
            callback()
        return [[1.0, 0.0] for _ in texts]


@pytest.fixture
def setup(tmp_path):
    engine = create_engine_for(tmp_path / "db.sqlite3")
    migrate(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as session, session.begin():
        candidate = Candidate(display_name="Test", status="AVAILABLE")
        blob = Blob(content_sha256="a" * 64, suffix=".pdf", size_bytes=1, storage_path="unused")
        document = ResumeDocument(candidate=candidate)
        revision = ResumeRevision(document=document, blob=blob, content_sha256="a" * 64,
            original_filename="test.pdf", status="READY", is_current=True,
            parsed_data={"name": "Test", "skills": ["Python"], "location": "上海", "preferred_location": "北京"})
        session.add(revision)
        session.flush()
        cid, rid = candidate.id, revision.id
        enqueue_sync(session, "candidate", cid)
    index = LanceDBSearchIndex(tmp_path / "index", vector_dimension=2)
    embedding = Embedding()
    service = IndexSyncService(session_factory=factory, index=index, embedding_provider=embedding)
    yield factory, cid, rid, index, embedding, service
    engine.dispose()


@pytest.mark.asyncio
async def test_sync_projects_current_revision_and_intent(setup):
    factory, cid, rid, index, _, service = setup
    assert await service.run_once() == 1
    hits = index.filter_search(CandidateFilters(preferred_locations=("北京",)), 10)
    assert [(h.candidate_id, h.revision_id) for h in hits] == [(cid, rid)]
    with factory() as session:
        job = session.scalar(select(IndexSyncRecord))
        assert job.applied_version == job.requested_version
        assert job.status == "SYNCED"


@pytest.mark.asyncio
async def test_sync_failure_remains_retryable_without_losing_work(setup):
    factory, _, _, index, embedding, service = setup
    embedding.fail = True
    assert await service.run_once() == 0
    with factory() as session:
        job = session.scalar(select(IndexSyncRecord))
        assert job.status == "RETRY_WAIT" and job.attempts == 1
    embedding.fail = False
    assert await service.run_once(force=True) == 1
    assert len(index.filter_search(CandidateFilters(), 10)) == 1


@pytest.mark.asyncio
async def test_delete_restore_sync_without_reparsing(setup):
    factory, cid, rid, index, _, service = setup
    await service.run_once()
    trash = SoftDeleteService(factory)
    trash.soft_delete("candidate", cid)
    await service.run_once()
    assert index.filter_search(CandidateFilters(), 10) == []
    trash.restore("candidate", cid)
    await service.run_once()
    assert [hit.revision_id for hit in index.filter_search(CandidateFilters(), 10)] == [rid]


@pytest.mark.asyncio
async def test_old_embedding_completion_cannot_publish_over_new_state(setup):
    factory, cid, _, index, embedding, service = setup
    def change_while_embedding():
        with factory() as session, session.begin():
            session.get(Candidate, cid).status = "ON_HOLD"
            enqueue_sync(session, "candidate", cid)
    embedding.before_return = change_while_embedding
    assert await service.run_once() == 0
    assert index.filter_search(CandidateFilters(), 10) == []
    assert await service.run_once() == 1
    assert index.filter_search(CandidateFilters(), 10) == []
    assert len(index.filter_search(CandidateFilters(candidate_status="ON_HOLD"), 10)) == 1


@pytest.mark.asyncio
async def test_enqueue_sync_mode_state_machine(tmp_path):
    engine = create_engine_for(tmp_path / "db.sqlite3")
    migrate(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as session, session.begin():
        c1 = Candidate(display_name="T1", status="AVAILABLE")
        c2 = Candidate(display_name="T2", status="AVAILABLE")
        session.add_all([c1, c2])
        session.flush()
        id1, id2 = c1.id, c2.id

    def job(entity_id):
        with factory() as session:
            j = session.scalar(select(IndexSyncRecord).where(
                IndexSyncRecord.entity_type == "candidate",
                IndexSyncRecord.entity_id == entity_id))
            return (j.requested_mode, j.requested_version, j.applied_version) if j else None

    # 1. 无记录 + METADATA → METADATA
    with factory() as session, session.begin():
        enqueue_sync(session, "candidate", id1, mode="METADATA")
    assert job(id1) == ("METADATA", 1, 0)

    # 2. 已同步（applied==requested）+ FULL → FULL
    with factory() as session, session.begin():
        session.scalar(select(IndexSyncRecord).where(IndexSyncRecord.entity_id == id1)).applied_version = 1
    with factory() as session, session.begin():
        enqueue_sync(session, "candidate", id1, mode="FULL")
    assert job(id1) == ("FULL", 2, 1)

    # 3. 已同步 FULL + METADATA → METADATA（关键回归）
    with factory() as session, session.begin():
        session.scalar(select(IndexSyncRecord).where(IndexSyncRecord.entity_id == id1)).applied_version = 2
    with factory() as session, session.begin():
        enqueue_sync(session, "candidate", id1, mode="METADATA")
    assert job(id1) == ("METADATA", 3, 2)

    # 4. 无记录 + FULL → FULL（用 id2 从无记录开始）
    with factory() as session, session.begin():
        enqueue_sync(session, "candidate", id2, mode="FULL")
    assert job(id2) == ("FULL", 1, 0)

    # 5. pending FULL + METADATA → FULL
    with factory() as session, session.begin():
        enqueue_sync(session, "candidate", id2, mode="METADATA")
    assert job(id2) == ("FULL", 2, 0)

    # 6. pending METADATA + FULL → FULL
    with factory() as session, session.begin():
        session.scalar(select(IndexSyncRecord).where(IndexSyncRecord.entity_id == id2)).requested_mode = "METADATA"
    with factory() as session, session.begin():
        enqueue_sync(session, "candidate", id2, mode="FULL")
    assert job(id2) == ("FULL", 3, 0)

    # 7. pending METADATA + METADATA → METADATA
    with factory() as session, session.begin():
        session.scalar(select(IndexSyncRecord).where(IndexSyncRecord.entity_id == id2)).requested_mode = "METADATA"
    with factory() as session, session.begin():
        enqueue_sync(session, "candidate", id2, mode="METADATA")
    assert job(id2) == ("METADATA", 4, 0)


@pytest.mark.asyncio
async def test_sync_retains_all_current_document_evidence(setup):
    factory, cid, rid, index, _, service = setup
    with factory() as session, session.begin():
        first = session.get(ResumeRevision, rid)
        second = ResumeRevision(document=ResumeDocument(candidate_id=cid), blob_id=first.blob_id,
            content_sha256='b' * 64, original_filename='other.pdf', status='READY', is_current=True,
            parsed_data={'skills': ['Rust'], 'preferred_locations': ['广州']})
        session.add(second)
        session.flush()
        second_id = second.id
        enqueue_sync(session, 'candidate', cid)
    assert await service.run_once() == 1
    assert index.get_revision_chunks(rid)
    assert index.get_revision_chunks(second_id)
    assert index.filter_search(CandidateFilters(preferred_locations=('北京',)), 10)
    assert index.filter_search(CandidateFilters(preferred_locations=('广州',)), 10)
