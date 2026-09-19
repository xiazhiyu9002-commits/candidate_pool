import inspect
from dataclasses import replace
from decimal import Decimal

import pytest
from sqlalchemy.orm import sessionmaker

from kerui_recruit.db.base import Base
from kerui_recruit.db.models import Blob, Candidate, Jd, JdRevision, ResumeDocument, ResumeRevision
from kerui_recruit.db.session import create_engine_for
from kerui_recruit.match.service import MatchService
from kerui_recruit.providers.fakes import FakeRerankerProvider
from kerui_recruit.search.contracts import CandidateFilters, SearchChunk
from kerui_recruit.search.lancedb_index import LanceDBSearchIndex
from kerui_recruit.search.service import HybridSearchService


class FixedEmbedding:
    async def embed_query(self, text):
        return [1., 0.]


def setup(tmp_path):
    engine = create_engine_for(tmp_path / "test.sqlite")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    candidate_index = LanceDBSearchIndex(tmp_path / "candidates", vector_dimension=2)
    with factory.begin() as session:
        session.add(Candidate(id="person", display_name="Person", status="AVAILABLE", total_years=Decimal("6"), highest_degree="MASTER"))
        session.add(Blob(id="blob", content_sha256="b" * 64, suffix=".txt", size_bytes=20, storage_path="blob"))
        session.flush()
        session.add(ResumeDocument(id="doc", candidate_id="person"))
        session.flush()
        session.add(ResumeRevision(id="resume", document_id="doc", blob_id="blob", content_sha256="b" * 64,
                                   original_filename="resume.txt", status="READY", is_current=True,
                                   raw_text="Python machine learning payments", parsed_data={"location": "上海", "direction": "BACKEND", "skills": ["Python"]}))
        for jid, content in (("a", "Python payments"), ("b", "statistical inference"), ("c", "graphic design")):
            session.add(Jd(id=jid, company=jid, title=content, status="OPEN"))
            session.flush()
            session.add(JdRevision(id=f"rev-{jid}", jd_id=jid, status="READY", is_current=True,
                                    source_text=content, min_years=Decimal("3"), highest_degree="BACHELOR",
                                    parsed_data={"summary": content, "required_skills": ["Python"], "direction": "BACKEND"}))
    candidate_index.upsert([SearchChunk("person-chunk", "person", "resume", "Python machine learning payments", (1., 0.),
                                        6, "MASTER", "上海", "AVAILABLE", direction="BACKEND")])
    service = HybridSearchService(index=candidate_index, embedding_provider=FixedEmbedding(),
                                  reranker_provider=FakeRerankerProvider())
    return factory, service


def jd_index(tmp_path):
    # Assert behavior's missing extension before importing it, to keep RED diagnostic clear.
    assert "jd_index" in inspect.signature(MatchService).parameters
    from kerui_recruit.match.jd_index import JdSearchIndex
    index = JdSearchIndex(tmp_path / "jobs", vector_dimension=2)
    for jid, content, vector in (("a", "Python payments", (1., 0.)),
                                  ("b", "statistical inference", (.99, .01)),
                                  ("c", "graphic design", (0., 1.))):
        index.upsert(jd_id=jid, revision_id=f"rev-{jid}", content=content, vector=vector, direction="BACKEND")
    return index


@pytest.mark.asyncio
async def test_reverse_recalls_jobs_semantically_once_without_searching_talent_pool(tmp_path):
    factory, search = setup(tmp_path)
    index = jd_index(tmp_path)
    service = MatchService(session_factory=factory, search_service=search, jd_index=index)
    async def forbidden(*args, **kwargs):
        raise AssertionError("Reverse matching must not call a talent-pool search")
    search.search = forbidden
    service.match_jd = forbidden
    records = await service.reverse_match_candidate("person", limit=2)
    assert {record.jd_id for record in records} == {"a", "b"}
    assert all(record.hit.candidate_id == "person" and record.hit.revision_id == "resume" for record in records)
    assert all(record.score.total == service.score(record.revision_id, record.hit).total for record in records)
    assert records[0].score.total >= records[1].score.total


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["closed", "failed", "old"])
async def test_reverse_checks_live_jobs_after_index_recall(tmp_path, change):
    factory, search = setup(tmp_path)
    index = jd_index(tmp_path)
    with factory.begin() as session:
        if change == "closed":
            session.get(Jd, "a").status = "FILLED"
        elif change == "failed":
            session.get(JdRevision, "rev-a").status = "FAILED"
        else:
            session.get(JdRevision, "rev-a").is_current = False
    records = await MatchService(session_factory=factory, search_service=search, jd_index=index).reverse_match_candidate("person")
    assert "a" not in {record.jd_id for record in records}


@pytest.mark.asyncio
async def test_reverse_rejects_candidate_on_hold(tmp_path):
    factory, search = setup(tmp_path)
    index = jd_index(tmp_path)
    with factory.begin() as session:
        session.get(Candidate, "person").status = "ON_HOLD"
    records = await MatchService(session_factory=factory, search_service=search, jd_index=index).reverse_match_candidate("person")
    assert records == []


@pytest.mark.asyncio
async def test_reverse_rejects_zero_must_skill(tmp_path):
    factory, search = setup(tmp_path)
    index = jd_index(tmp_path)
    with factory.begin() as session:
        revision = session.get(JdRevision, "rev-a")
        parsed = dict(revision.parsed_data or {})
        parsed["required_skills"] = ["Spring"]  # 候选人只有 Python，零命中 Spring
        parsed["industry"] = "金融"  # 行业匹配，抬高总分，暴露「总分补偿 MUST 缺口」的旧路径
        revision.parsed_data = parsed
        resume = session.get(ResumeRevision, "resume")
        rparsed = dict(resume.parsed_data or {})
        rparsed["industry"] = "金融"
        resume.parsed_data = rparsed
    records = await MatchService(session_factory=factory, search_service=search, jd_index=index).reverse_match_candidate("person")
    assert "a" not in {record.jd_id for record in records}


@pytest.mark.asyncio
async def test_jd_index_replacement_and_deletion_change_reverse_recall(tmp_path):
    factory, search = setup(tmp_path)
    index = jd_index(tmp_path)
    index.delete_jd("a")
    index.delete_jd("c")
    index.upsert(jd_id="b", revision_id="rev-b-new", content="Python", vector=(1., 0.), direction="BACKEND")
    assert index.index.get_revision_chunks("rev-b") == []
    with factory.begin() as session:
        old = session.get(JdRevision, "rev-b")
        old.is_current = False
        session.add(JdRevision(id="rev-b-new", jd_id="b", status="READY", is_current=True, source_text="Python",
                               parsed_data={"direction": "BACKEND"}))
    records = await MatchService(session_factory=factory, search_service=search, jd_index=index).reverse_match_candidate("person")
    assert [record.revision_id for record in records] == ["rev-b-new"]


@pytest.mark.asyncio
async def test_forward_match_never_returns_noncurrent_or_held_candidates(tmp_path):
    factory, search = setup(tmp_path)
    service = MatchService(session_factory=factory, search_service=search)
    with factory.begin() as session:
        session.get(Candidate, "person").status = "ON_HOLD"
    page = await service.match_jd(revision_id="rev-a")
    assert page.items == ()


@pytest.mark.asyncio
async def test_closed_job_cannot_start_forward_match(tmp_path):
    factory, search = setup(tmp_path)
    service = MatchService(session_factory=factory, search_service=search)
    with factory.begin() as session:
        session.get(Jd, "a").status = "FILLED"
    page = await service.match_jd(revision_id="rev-a")
    assert page.items == ()
    assert page.empty_reason == "jd_not_eligible"


@pytest.mark.asyncio
@pytest.mark.parametrize("direction", [None, "OTHER", "OPERATIONS"])
async def test_forward_match_does_not_block_unconfirmed_jd_direction(tmp_path, direction):
    factory, search = setup(tmp_path)
    service = MatchService(session_factory=factory, search_service=search)
    with factory.begin() as session:
        revision = session.get(JdRevision, "rev-a")
        parsed = dict(revision.parsed_data or {})
        parsed["direction"] = direction
        revision.parsed_data = parsed
    page = await service.match_jd(revision_id="rev-a")
    # JD 方向未确认不硬过滤：仍按技能/职责返回候选人。
    assert [hit.candidate_id for hit in page.items] == ["person"]


@pytest.mark.asyncio
async def test_match_jd_degrades_gracefully_on_scoring_error(tmp_path):
    factory, search = setup(tmp_path)
    service = MatchService(session_factory=factory, search_service=search)

    def fail(*args, **kwargs):
        raise RuntimeError("scoring failed")

    service._score_and_sort = fail  # type: ignore[method-assign]
    page = await service.match_jd(revision_id="rev-a")
    # 评分异常时优雅降级：仍返回经资格（evaluate_pair）过滤的召回结果，而不是整条失败为 0。
    assert [hit.candidate_id for hit in page.items] == ["person"]
    assert "SCORING_UNAVAILABLE" in page.degraded_reasons


@pytest.mark.asyncio
@pytest.mark.parametrize("direction", [None, "OTHER"])
async def test_forward_match_does_not_block_unconfirmed_direction(tmp_path, direction):
    factory, search = setup(tmp_path)
    service = MatchService(session_factory=factory, search_service=search)
    with factory.begin() as session:
        resume = session.get(ResumeRevision, "resume")
        parsed = dict(resume.parsed_data or {})
        parsed["direction"] = direction  # 当前结构化数据方向未确认，索引仍是 BACKEND
        resume.parsed_data = parsed
    page = await service.match_jd(revision_id="rev-a")
    # 方向未确认不硬过滤：仍按技能/职责返回候选人。
    assert [hit.candidate_id for hit in page.items] == ["person"]


@pytest.mark.asyncio
@pytest.mark.parametrize("direction", [None, "OTHER"])
async def test_reverse_does_not_block_unconfirmed_direction(tmp_path, direction):
    factory, search = setup(tmp_path)
    index = jd_index(tmp_path)
    with factory.begin() as session:
        resume = session.get(ResumeRevision, "resume")
        parsed = dict(resume.parsed_data or {})
        parsed["direction"] = direction
        resume.parsed_data = parsed
    records = await MatchService(session_factory=factory, search_service=search, jd_index=index).reverse_match_candidate("person")
    # 方向未确认不硬过滤：仍返回岗位。
    assert records != []


@pytest.mark.asyncio
async def test_record_run_rechecks_job_and_candidate_after_recall(tmp_path):
    factory, search = setup(tmp_path)
    service = MatchService(session_factory=factory, search_service=search)
    page = await service.match_jd(revision_id="rev-a")
    assert page.items
    with factory.begin() as session:
        session.get(Jd, "a").status = "FILLED"
    with pytest.raises(ValueError, match="eligible"):
        service.record_run(revision_id="rev-a", hits=page.items)


@pytest.mark.asyncio
async def test_record_reverse_run_rechecks_candidate_after_recall(tmp_path):
    factory, search = setup(tmp_path)
    service = MatchService(session_factory=factory, search_service=search, jd_index=jd_index(tmp_path))
    records = await service.reverse_match_candidate("person")
    assert records
    with factory.begin() as session:
        session.get(Candidate, "person").status = "ON_HOLD"
    with pytest.raises(ValueError, match="eligible"):
        service.record_reverse_run(candidate_id="person", records=records)


@pytest.mark.asyncio
async def test_reverse_missing_job_projection_is_not_reported_as_no_matches(tmp_path):
    factory, search = setup(tmp_path)
    service = MatchService(session_factory=factory, search_service=search)
    with pytest.raises(RuntimeError, match="index"):
        await service.reverse_match_candidate("person")


@pytest.mark.asyncio
async def test_reverse_empty_job_index_with_open_jobs_is_unavailable(tmp_path):
    factory, search = setup(tmp_path)
    from kerui_recruit.match.jd_index import JdSearchIndex
    service = MatchService(session_factory=factory, search_service=search,
                           jd_index=JdSearchIndex(tmp_path / "empty", vector_dimension=2))
    with pytest.raises(RuntimeError, match="index"):
        await service.reverse_match_candidate("person")


@pytest.mark.asyncio
async def test_reverse_no_eligible_jobs_is_legitimate_empty_result(tmp_path):
    factory, search = setup(tmp_path)
    with factory.begin() as session:
        from sqlalchemy import update
        session.execute(update(Jd).values(status="FILLED"))
    assert await MatchService(session_factory=factory, search_service=search).reverse_match_candidate("person") == []


@pytest.mark.asyncio
async def test_reverse_all_deleted_projection_rows_with_open_jobs_is_unavailable(tmp_path):
    factory, search = setup(tmp_path)
    index = jd_index(tmp_path)
    for jid in ("a", "b", "c"):
        index.delete_jd(jid)
    with pytest.raises(RuntimeError, match="index"):
        await MatchService(session_factory=factory, search_service=search, jd_index=index).reverse_match_candidate("person")


@pytest.mark.asyncio
async def test_reverse_does_not_return_jobs_waiting_for_same_revision_projection(tmp_path):
    factory, search = setup(tmp_path)
    index = jd_index(tmp_path)
    from kerui_recruit.search.sync import enqueue_sync
    with factory.begin() as session:
        enqueue_sync(session, "jd", "a")
    records = await MatchService(session_factory=factory, search_service=search, jd_index=index).reverse_match_candidate("person")
    assert "a" not in {record.jd_id for record in records}


@pytest.mark.asyncio
async def test_forward_record_rejects_candidate_pending_same_revision_projection(tmp_path):
    factory, search = setup(tmp_path)
    service = MatchService(session_factory=factory, search_service=search)
    page = await service.match_jd(revision_id="rev-a")
    from kerui_recruit.search.sync import enqueue_sync
    with factory.begin() as session:
        enqueue_sync(session, "candidate", "person")
    with pytest.raises(ValueError, match="eligible"):
        service.record_run(revision_id="rev-a", hits=page.items)


@pytest.mark.asyncio
async def test_forward_matching_rejects_jd_pending_projection(tmp_path):
    factory, search = setup(tmp_path)
    from kerui_recruit.search.sync import enqueue_sync
    with factory.begin() as session:
        enqueue_sync(session, "jd", "a")
    page = await MatchService(session_factory=factory, search_service=search).match_jd(revision_id="rev-a")
    assert page.items == ()


@pytest.mark.asyncio
async def test_reverse_all_open_jobs_pending_sync_reports_unavailable(tmp_path):
    factory, search = setup(tmp_path)
    index = jd_index(tmp_path)
    from kerui_recruit.search.sync import enqueue_sync
    with factory.begin() as session:
        for jid in ("a", "b", "c"):
            enqueue_sync(session, "jd", jid)
    with pytest.raises(RuntimeError, match="synchronization"):
        await MatchService(session_factory=factory, search_service=search, jd_index=index).reverse_match_candidate("person")


def test_candidate_representation_separates_keyword_and_vector_text(tmp_path):
    """反向匹配的 keyword 用结构化关键词，vector 用 AI 画像/向量文本，二者不同。"""
    engine = create_engine_for(tmp_path / "test.sqlite")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory.begin() as session:
        session.add(Candidate(id="person", display_name="张三", status="AVAILABLE",
                              total_years=Decimal("6"), highest_degree="MASTER"))
        session.add(Blob(id="blob", content_sha256="b" * 64, suffix=".txt", size_bytes=20, storage_path="blob"))
        session.flush()
        session.add(ResumeDocument(id="doc", candidate_id="person"))
        session.flush()
        session.add(ResumeRevision(id="resume", document_id="doc", blob_id="blob", content_sha256="b" * 64,
                                   original_filename="resume.txt", status="READY", is_current=True,
                                   raw_text="Python", parsed_data={
                                       "name": "张三", "skills": ["Python", "Java"],
                                       "ai_profile_summary": "资深后端工程师",
                                       "current_company": "腾讯科技", "current_title": "工程师",
                                       "location": "上海",
                                   }))
    index = LanceDBSearchIndex(tmp_path / "candidates", vector_dimension=2)
    search = HybridSearchService(index=index, embedding_provider=FixedEmbedding(),
                                 reranker_provider=FakeRerankerProvider())
    service = MatchService(session_factory=factory, search_service=search)
    hit, _ = service._candidate_representation("person")
    assert "Python" in hit.content
    assert "腾讯科技" in hit.content
    assert hit.vector_text and "资深后端工程师" in hit.vector_text
    assert hit.content != hit.vector_text


@pytest.mark.asyncio
async def test_reverse_match_passes_mode_specific_query_texts(tmp_path):
    """keyword/vector/hybrid 传给搜索服务的 query/vector_query 必须真正区分。"""
    from kerui_recruit.match.jd_index import JdSearchIndex
    from kerui_recruit.search.contracts import SearchPage

    engine = create_engine_for(tmp_path / "test.sqlite")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory.begin() as session:
        session.add(Candidate(id="person", display_name="张三", status="AVAILABLE",
                              total_years=Decimal("6"), highest_degree="MASTER"))
        session.add(Blob(id="blob", content_sha256="b" * 64, suffix=".txt", size_bytes=20, storage_path="blob"))
        session.flush()
        session.add(ResumeDocument(id="doc", candidate_id="person"))
        session.flush()
        session.add(ResumeRevision(id="resume", document_id="doc", blob_id="blob", content_sha256="b" * 64,
                                   original_filename="resume.txt", status="READY", is_current=True,
                                   raw_text="Python", parsed_data={
                                       "name": "张三", "skills": ["Python", "Java"],
                                       "ai_profile_summary": "资深后端工程师",
                                       "current_company": "腾讯科技", "current_title": "工程师",
                                       "location": "上海", "direction": "BACKEND",
                                   }))
        session.add(Jd(id="a", company="a", title="Python payments", status="OPEN"))
        session.flush()
        session.add(JdRevision(id="rev-a", jd_id="a", status="READY", is_current=True,
                               source_text="Python payments", min_years=Decimal("3"),
                               highest_degree="BACHELOR",
                               parsed_data={"summary": "Python payments", "required_skills": ["Python"], "direction": "BACKEND"}))

    index = LanceDBSearchIndex(tmp_path / "candidates", vector_dimension=2)
    search = HybridSearchService(index=index, embedding_provider=FixedEmbedding(),
                                 reranker_provider=FakeRerankerProvider())
    jd = JdSearchIndex(tmp_path / "jobs", vector_dimension=2)
    jd.upsert(jd_id="a", revision_id="rev-a", content="Python payments", vector=(1., 0.), direction="BACKEND")
    service = MatchService(session_factory=factory, search_service=search, jd_index=jd)

    calls: list[dict] = []

    class Spy:
        async def search(self, query, filters, *, limit, mode, deadline=None, observer=None, vector_query=None):
            calls.append({"query": query, "mode": mode, "vector_query": vector_query})
            return SearchPage(items=())

    service.reverse_search = Spy()

    for mode in ("keyword", "vector", "hybrid"):
        await service.reverse_match_candidate("person", mode=mode)

    # 双通道召回会为每种模式发起同向 + 救援两次搜索；这里取每个模式的同向通道调用。
    def first(mode):
        return next(c for c in calls if c["mode"] == mode)

    keyword_call, vector_call, hybrid_call = first("keyword"), first("vector"), first("hybrid")
    # keyword 只用结构化关键词，不传向量文本。
    assert keyword_call["mode"] == "keyword"
    assert "Python" in keyword_call["query"] and "腾讯科技" in keyword_call["query"]
    assert keyword_call["vector_query"] is None
    # vector 用 AI 画像/向量文本。
    assert vector_call["mode"] == "vector"
    assert "资深后端工程师" in vector_call["query"]
    assert vector_call["vector_query"] is None
    # hybrid 分别用关键词做 FTS、向量文本做 embedding。
    assert hybrid_call["mode"] == "hybrid"
    assert "Python" in hybrid_call["query"] and "腾讯科技" in hybrid_call["query"]
    assert hybrid_call["vector_query"] and "资深后端工程师" in hybrid_call["vector_query"]
