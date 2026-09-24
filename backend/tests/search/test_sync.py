from pathlib import Path
import asyncio
import time

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from kerui_recruit.db.migrate import migrate
from kerui_recruit.db.models import Blob, Candidate, IndexSyncRecord, ResumeDocument, ResumeRevision
from kerui_recruit.db.session import create_engine_for
from kerui_recruit.search.cities import normalize_location_terms
from kerui_recruit.search.contracts import CandidateFilters
from kerui_recruit.search.lancedb_index import LanceDBSearchIndex
from kerui_recruit.search.sync import IndexSyncService, _TokenRateLimiter, enqueue_sync
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


class _ConcurrencyEmbedding(Embedding):
    """记录同时在飞的 embedding 请求数：验证投影确实按 concurrency 并发。"""

    def __init__(self, *, delay: float = 0.05) -> None:
        super().__init__()
        self.delay = delay
        self.in_flight = 0
        self.peak = 0

    async def embed_documents(self, texts):
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            await asyncio.sleep(self.delay)
            return [[1.0, 0.0] for _ in texts]
        finally:
            self.in_flight -= 1


def _seed_candidates(factory, count: int) -> None:
    with factory() as session, session.begin():
        for index in range(count):
            candidate = Candidate(display_name=f"C{index}", status="AVAILABLE")
            session.add(ResumeRevision(
                document=ResumeDocument(candidate=candidate),
                blob=Blob(content_sha256=f"{index:064d}", suffix=".pdf", size_bytes=1,
                          storage_path=f"unused-{index}"),
                content_sha256=f"{index:064d}", original_filename=f"{index}.pdf",
                status="READY", is_current=True,
                parsed_data={"name": f"C{index}", "skills": ["Python"]},
            ))
            session.flush()
            enqueue_sync(session, "candidate", candidate.id)


@pytest.mark.asyncio
async def test_sync_projects_concurrently_up_to_the_limit(tmp_path):
    engine = create_engine_for(tmp_path / "db.sqlite3")
    migrate(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    _seed_candidates(factory, 6)
    index = LanceDBSearchIndex(tmp_path / "index", vector_dimension=2)
    embedding = _ConcurrencyEmbedding()
    service = IndexSyncService(
        session_factory=factory, index=index, embedding_provider=embedding,
        tokens_per_minute=10 ** 9,  # 这条只验并发，不验预算
    )

    assert await service.run_once(batch_size=10, concurrency=3) == 6
    assert embedding.peak == 3
    engine.dispose()


@pytest.mark.asyncio
async def test_sync_publishes_serially_even_with_parallel_workers(tmp_path, monkeypatch):
    """发布必须串行：``_publish`` 用 BEGIN IMMEDIATE 独占 SQLite 写锁，事务里还夹着一次
    LanceDB 提交，锁持有时间远大于 busy_timeout(5s)。并发 worker 同时进入会互相超时
    （表现为 outbox 长期 RETRY_WAIT + last_error=OperationalError），快照与 embedding 仍并发。
    """
    engine = create_engine_for(tmp_path / "db.sqlite3")
    migrate(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    _seed_candidates(factory, 6)
    index = LanceDBSearchIndex(tmp_path / "index", vector_dimension=2)
    service = IndexSyncService(
        session_factory=factory, index=index, embedding_provider=_ConcurrencyEmbedding(),
        tokens_per_minute=10 ** 9,
    )

    state = {"in_flight": 0, "peak": 0}
    original = service._publish

    def counting_publish(*args, **kwargs):
        state["in_flight"] += 1
        state["peak"] = max(state["peak"], state["in_flight"])
        try:
            time.sleep(0.05)  # 放大锁持有窗口，未串行时这里必然被并发进入
            return original(*args, **kwargs)
        finally:
            state["in_flight"] -= 1

    monkeypatch.setattr(service, "_publish", counting_publish)
    assert await service.run_once(batch_size=10, concurrency=3) == 6
    assert state["peak"] == 1
    engine.dispose()


@pytest.mark.asyncio
async def test_sync_normalizes_location_terms_from_the_location_column(tmp_path):
    """location_terms 必须与 location 列同源且已归一化。

    现居硬过滤读 ``location_terms``、界面显示 ``location``：两者不同源时会出现
    「显示苏州、按无锡筛」（实测 6/1477 行）。非规范写法（``广东省深圳市``）也必须
    归一化，否则 ``array_has_any(["深圳"])`` 永远召不回这些行。
    """
    engine = create_engine_for(tmp_path / "db.sqlite3")
    migrate(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as session, session.begin():
        candidate = Candidate(display_name="C0", status="AVAILABLE")
        revision = ResumeRevision(
            document=ResumeDocument(candidate=candidate),
            blob=Blob(content_sha256="0" * 64, suffix=".pdf", size_bytes=1, storage_path="unused"),
            content_sha256="0" * 64, original_filename="0.pdf", status="READY", is_current=True,
            parsed_data={"name": "C0", "location": "广东省深圳市", "skills": ["Java"]},
        )
        session.add(revision)
        session.flush()
        enqueue_sync(session, "candidate", candidate.id)
        revision_id = revision.id

    index = LanceDBSearchIndex(tmp_path / "index", vector_dimension=2)
    service = IndexSyncService(session_factory=factory, index=index,
                               embedding_provider=Embedding(), tokens_per_minute=10 ** 9)
    assert await service.run_once(batch_size=10) == 1

    rows = [row for row in index.get_revision_chunks(revision_id) if row["chunk_type"] == "parent"]
    assert rows, "父 chunk 应当已发布"
    terms = set(rows[0]["location_terms"])
    assert "深圳" in terms
    # 同源不变量：按 location 列归一化出来的城市必须在词项里。
    assert set(normalize_location_terms(rows[0]["location"])) <= terms
    engine.dispose()


@pytest.mark.asyncio
async def test_sync_charges_embedding_budget_per_request(tmp_path):
    """每次 embedding 前先向 token 预算申请额度：预算是按请求文本量扣的。"""
    engine = create_engine_for(tmp_path / "db.sqlite3")
    migrate(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    _seed_candidates(factory, 3)
    index = LanceDBSearchIndex(tmp_path / "index", vector_dimension=2)
    service = IndexSyncService(
        session_factory=factory, index=index, embedding_provider=Embedding(),
        tokens_per_minute=10 ** 9,
    )
    charged: list[float] = []

    class _RecordingBudget:
        async def acquire(self, tokens: float) -> None:
            charged.append(tokens)

    service._embedding_budget = _RecordingBudget()

    assert await service.run_once(batch_size=10, concurrency=3) == 3
    assert len(charged) == 3
    assert all(cost > 0 for cost in charged)
    engine.dispose()


@pytest.mark.asyncio
async def test_token_rate_limiter_spends_budget_over_time():
    """桶满时立即放行；用光后按 预算/60 的速度补充（1 token/秒 → 再要 1 个要等约 1 秒）。"""
    limiter = _TokenRateLimiter(60)
    await limiter.acquire(60)

    started = time.monotonic()
    await limiter.acquire(1)
    assert time.monotonic() - started >= 0.8
