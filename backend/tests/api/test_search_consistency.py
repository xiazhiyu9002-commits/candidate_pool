from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import event
from sqlalchemy.orm import sessionmaker

from kerui_recruit.api.search import router
from kerui_recruit.db.base import Base
from kerui_recruit.db.models import Blob, Candidate, CandidateContact, ResumeDocument, ResumeRevision
from kerui_recruit.db.session import create_engine_for
from kerui_recruit.providers.fakes import FakeRerankerProvider
from kerui_recruit.search.contracts import SearchChunk, SearchPage
from kerui_recruit.search.lancedb_index import LanceDBSearchIndex
from kerui_recruit.search.service import HybridSearchService


class Embedding:
    async def embed_query(self, text):
        return [1., 0.]


def setup_app(tmp_path, count=1):
    engine = create_engine_for(tmp_path / "test.sqlite")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    index = LanceDBSearchIndex(tmp_path / "index", vector_dimension=2)
    with factory.begin() as session:
        for number in range(count):
            cid = f"c{number}"
            session.add(Candidate(id=cid, display_name=cid, status="AVAILABLE"))
            session.add(Blob(id=f"b{number}", content_sha256=str(number).zfill(64), suffix=".txt", size_bytes=6,
                             storage_path=f"blob-{number}", reference_count=1))
            session.flush()
            session.add(ResumeDocument(id=f"d{number}", candidate_id=cid))
            session.flush()
            session.add(ResumeRevision(id=f"r{number}", document_id=f"d{number}", blob_id=f"b{number}",
                                       content_sha256=str(number).zfill(64), original_filename=f"{number}.txt",
                                       status="READY", is_current=True, raw_text="Python"))
    index.upsert([SearchChunk(f"chunk{n}", f"c{n}", f"r{n}", "Python", (1., 0.), 5,
                             "MASTER", "上海", "AVAILABLE", preferred_location="广州") for n in range(count)])
    app = FastAPI()
    app.include_router(router)
    app.state.services = SimpleNamespace(session_factory=factory, encryption_service=None,
        search_service=HybridSearchService(index=index, embedding_provider=Embedding(),
                                          reranker_provider=FakeRerankerProvider()))
    return app, factory, engine


async def post(app, **payload):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local") as client:
        response = await client.post("/api/search/candidates", json=payload)
        assert response.status_code == 200, response.text
        return response.json()


@pytest.mark.asyncio
async def test_http_explicit_filters_override_whole_location_group_and_false_values(tmp_path):
    app, _, _ = setup_app(tmp_path)
    captured = []
    class Capture:
        search_timeout = 4.5
        async def search(self, query, filters, **kwargs):
            captured.append(filters)
            return SearchPage(items=())
    app.state.services.search_service = Capture()
    await post(app, query="仅本科 上海或北京 意向深圳或杭州", filters={
        "degree_exact": False, "locations": ["广州"], "preferred_locations": ["成都", "武汉"]})
    filters = captured[0]
    assert filters.degree_exact is False
    assert filters.location_values() == ("广州",)
    assert filters.preferred_location_values() == ("成都", "武汉")


@pytest.mark.asyncio
async def test_http_explicit_empty_and_null_filters_clear_parsed_conditions(tmp_path):
    app, _, _ = setup_app(tmp_path)
    captured = []
    class Capture:
        search_timeout = 4.5
        async def search(self, query, filters, **kwargs):
            captured.append(filters)
            return SearchPage(items=())
    app.state.services.search_service = Capture()
    await post(app, query="上海 意向深圳 本科 排除Java", filters={
        "locations": [], "preferred_locations": [], "highest_degree": None, "exclude_skills": []})
    filters = captured[0]
    assert filters.location_values() == ()
    assert filters.preferred_location_values() == ()
    assert filters.highest_degree is None
    assert filters.exclude_skills == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["deleted", "status", "revision_old", "revision_failed", "wrong_owner"])
async def test_http_rejects_stale_projection_against_live_sqlite(tmp_path, invalid):
    app, factory, _ = setup_app(tmp_path, 2)
    with factory.begin() as session:
        candidate = session.get(Candidate, "c0")
        revision = session.get(ResumeRevision, "r0")
        if invalid == "deleted":
            candidate.deleted_at = datetime.now(timezone.utc)
        elif invalid == "status":
            candidate.status = "ON_HOLD"
        elif invalid == "revision_old":
            revision.is_current = False
        elif invalid == "revision_failed":
            revision.status = "FAILED"
        else:
            revision.document_id = "d1"
    result = await post(app, query="Python")
    assert [item["candidate_id"] for item in result["items"]] == ["c1"]


@pytest.mark.asyncio
async def test_http_hydration_uses_bounded_batch_queries(tmp_path):
    app, _, engine = setup_app(tmp_path, 25)
    statements = []
    @event.listens_for(engine, "before_cursor_execute")
    def count_queries(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)
    result = await post(app, query="Python", limit=25)
    assert len(result["items"]) == 25
    assert len(statements) <= 3


@pytest.mark.asyncio
async def test_http_exclusion_verifies_all_current_documents_not_only_indexed_evidence(tmp_path):
    app, factory, _ = setup_app(tmp_path)
    with factory.begin() as session:
        session.add(ResumeDocument(id="extra-doc", candidate_id="c0"))
        session.flush()
        session.add(ResumeRevision(id="extra-revision", document_id="extra-doc", blob_id="b0",
                                   content_sha256="extra", original_filename="extra.txt", status="READY",
                                   is_current=True, raw_text="Java legacy project"))
    result = await post(app, query="Python 排除Java")
    assert result["items"] == []


@pytest.mark.asyncio
async def test_http_missing_current_exclusion_evidence_is_explicitly_unverified(tmp_path):
    app, factory, _ = setup_app(tmp_path)
    with factory.begin() as session:
        session.get(ResumeRevision, "r0").raw_text = None
    result = await post(app, query="Python 排除Java")
    assert result["items"] == []
    assert "EXCLUSION_UNVERIFIED" in result["degraded_reasons"]
    assert result["empty_reason"] == "service_error"


@pytest.mark.asyncio
async def test_http_pending_projection_never_returns_stale_same_revision_filters(tmp_path):
    app, factory, _ = setup_app(tmp_path)
    from kerui_recruit.search.sync import enqueue_sync
    with factory.begin() as session:
        session.get(Candidate, "c0").total_years = 1
        enqueue_sync(session, "candidate", "c0")
    result = await post(app, query="Python", filters={"min_years": 5})
    assert result["items"] == []


@pytest.mark.asyncio
async def test_http_dedup_candidates_sharing_contact_fingerprint(tmp_path):
    app, factory, _ = setup_app(tmp_path, 2)
    with factory.begin() as session:
        session.add(CandidateContact(candidate_id="c0", phone_fingerprint="13800138000"))
        session.add(CandidateContact(candidate_id="c1", phone_fingerprint="13800138000"))
    result = await post(app, query="Python")
    assert [item["candidate_id"] for item in result["items"]] == ["c0"]


@pytest.mark.asyncio
async def test_phone_filter_finds_candidate_beyond_recall_cap(tmp_path):
    """手机号下沉到数据库层后，超过 200 名候选人时排在后面的目标仍能命中。"""
    app, factory, _ = setup_app(tmp_path, 205)
    with factory.begin() as session:
        session.add(CandidateContact(candidate_id="c204", phone_fingerprint="13900000000"))
    result = await post(app, query="", filters={"phone": "13900000000"})
    assert [item["candidate_id"] for item in result["items"]] == ["c204"]


@pytest.mark.asyncio
async def test_gender_filter_finds_candidate_beyond_recall_cap(tmp_path):
    """性别下沉到数据库层后，超过 200 名候选人时排在后面的目标仍能命中。"""
    app, factory, _ = setup_app(tmp_path, 205)
    with factory.begin() as session:
        session.get(ResumeRevision, "r204").parsed_data = {"gender": "女"}
    result = await post(app, query="", filters={"gender": "女"})
    assert [item["candidate_id"] for item in result["items"]] == ["c204"]


async def post_raw(app, **payload):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local") as client:
        return await client.post("/api/search/candidates", json=payload)


@pytest.mark.asyncio
@pytest.mark.parametrize("operator", ["smart", "and", "or"])
async def test_keyword_operator_combos_accepted(tmp_path, operator):
    app, _, _ = setup_app(tmp_path)
    response = await post_raw(app, query="Python", mode="keyword", operator=operator, rewrite_enabled=False)
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_keyword_rewrite_true_rejected(tmp_path):
    app, _, _ = setup_app(tmp_path)
    response = await post_raw(app, query="Python", mode="keyword", rewrite_enabled=True)
    assert response.status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,operator", [("vector", "and"), ("vector", "or"), ("hybrid", "and"), ("hybrid", "or")])
async def test_vector_hybrid_non_smart_rejected(tmp_path, mode, operator):
    app, _, _ = setup_app(tmp_path)
    response = await post_raw(app, query="Python", mode=mode, operator=operator)
    assert response.status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,rewrite_enabled", [("vector", False), ("vector", True), ("hybrid", False), ("hybrid", True)])
async def test_vector_hybrid_smart_accepted(tmp_path, mode, rewrite_enabled):
    app, _, _ = setup_app(tmp_path)
    response = await post_raw(app, query="Python", mode=mode, operator="smart", rewrite_enabled=rewrite_enabled)
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_and_operator_forwards_keywords_filters_and_concepts(tmp_path):
    app, _, _ = setup_app(tmp_path)
    captured = []
    class Capture:
        search_timeout = 4.5
        async def search(self, query, filters, **kwargs):
            captured.append((query, filters, kwargs))
            return SearchPage(items=())
    app.state.services.search_service = Capture()
    response = await post_raw(app, query="JS 后端 5年以上 上海", mode="keyword", operator="and")
    assert response.status_code == 200
    query, filters, kwargs = captured[0]
    assert query == "JS 后端"
    assert filters.min_years == 5
    assert filters.location_values() == ("上海",)
    assert kwargs["operator"] == "and"
    concepts = kwargs["concepts"]
    assert {c.canonical for c in concepts} == {"JavaScript", "后端"}


@pytest.mark.asyncio
async def test_school_alias_resolves_to_concept(tmp_path):
    from kerui_recruit.schools.seed import seed_schools
    app, factory, _ = setup_app(tmp_path)
    with factory.begin() as session:
        seed_schools(session)
    captured = []
    class Capture:
        search_timeout = 4.5
        async def search(self, query, filters, **kwargs):
            captured.append((query, filters, kwargs))
            return SearchPage(items=())
    app.state.services.search_service = Capture()
    response = await post_raw(app, query="北大 Java", mode="keyword", operator="and")
    assert response.status_code == 200
    _, _, kwargs = captured[0]
    concepts = kwargs["concepts"]
    canonicals = {c.canonical for c in concepts}
    assert "北京大学" in canonicals
    assert "Java" in canonicals
    school_concept = next(c for c in concepts if c.canonical == "北京大学")
    assert "北大" in school_concept.aliases


@pytest.mark.asyncio
async def test_success_response_includes_query_plan(tmp_path):
    app, _, _ = setup_app(tmp_path)
    result = await post(app, query="Python")
    assert result["query_plan"]["operator"] == "smart"
    assert result["query_plan"]["rewrite_requested"] is False
    assert result["query_plan"]["rewrite_status"] == "disabled"


@pytest.mark.asyncio
async def test_keyword_response_query_plan_not_applicable(tmp_path):
    app, _, _ = setup_app(tmp_path)
    result = await post(app, query="Python", mode="keyword", operator="and")
    assert result["query_plan"]["operator"] == "and"
    assert result["query_plan"]["rewrite_status"] == "not_applicable"


@pytest.mark.asyncio
async def test_no_match_response_includes_query_plan(tmp_path):
    app, _, _ = setup_app(tmp_path)
    class Capture:
        search_timeout = 4.5
        async def search(self, query, filters, **kwargs):
            return SearchPage(items=())
    app.state.services.search_service = Capture()
    result = await post(app, query="Python")
    assert result["items"] == []
    assert result["query_plan"]["operator"] == "smart"
    assert result["query_plan"]["rewrite_status"] == "disabled"


@pytest.mark.asyncio
async def test_query_plan_echoes_parsed_conditions_and_retained_keywords(tmp_path):
    app, _, _ = setup_app(tmp_path)
    result = await post(app, query="现居上海 Python 5年以上")
    plan = result["query_plan"]
    assert plan["retained_keywords"] == "Python"
    fields = {(c["field"], c["value"], c["confidence"]) for c in plan["parsed_conditions"]}
    assert ("location", "上海", "explicit") in fields
    assert ("min_years", "5年", "explicit") in fields


@pytest.mark.asyncio
async def test_query_plan_school_entity_not_treated_as_location(tmp_path):
    app, _, _ = setup_app(tmp_path)
    result = await post(app, query="北京大学 后端")
    plan = result["query_plan"]
    assert plan["retained_keywords"] == "北京大学 后端"
    assert all(c["field"] != "location" for c in plan["parsed_conditions"])


@pytest.mark.asyncio
async def test_filter_only_pagination_has_no_overlap_and_has_more(tmp_path):
    app, _, _ = setup_app(tmp_path, 25)
    first = await post(app, query="", limit=10, offset=0)
    second = await post(app, query="", limit=10, offset=10)
    assert len(first["items"]) == 10
    assert first["has_more"] is True
    first_ids = {i["candidate_id"] for i in first["items"]}
    second_ids = {i["candidate_id"] for i in second["items"]}
    assert len(second_ids) == 10
    assert not (first_ids & second_ids)
