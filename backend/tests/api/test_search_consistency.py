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


def parent_and_child_service(tmp_path):
    """候选人 c0 有真实 parent 与一条只含查询词的 child 片段。"""
    index = LanceDBSearchIndex(tmp_path / "index-e", vector_dimension=2)
    index.upsert([
        SearchChunk("p0", "c0", "r0", "父概况文本", (0., 1.), 5, "MASTER", "上海", "AVAILABLE",
                    keyword_text="父概况文本", keyword_index_text="父 概况 文本",
                    chunk_type="parent", kind="parent"),
        SearchChunk("ch0", "c0", "r0", "Kafka 支付链路", (1., 0.), 5, "MASTER", "上海", "AVAILABLE",
                    keyword_text="Kafka 支付链路", keyword_index_text="kafka 支付 链路",
                    chunk_type="child", kind="project", parent_id="r0"),
    ])
    return HybridSearchService(index=index, embedding_provider=Embedding(),
                               reranker_provider=FakeRerankerProvider())


@pytest.mark.asyncio
async def test_api_content_is_parent_overview_and_reasons_come_from_evidence(tmp_path):
    """查询词只出现在 child 时，content 仍是 parent 概况，reasons 从证据生成。"""
    app, _, _ = setup_app(tmp_path)
    app.state.services.search_service = parent_and_child_service(tmp_path)
    result = await post(app, query="Kafka", mode="keyword", search_body=True)
    assert [item["candidate_id"] for item in result["items"]] == ["c0"]
    item = result["items"][0]
    assert item["content"] == "父概况文本"
    assert any("Kafka" in reason and "project" in reason for reason in item["reasons"])


@pytest.mark.asyncio
async def test_hybrid_search_body_recalls_child_only_matches(tmp_path):
    """混合模式同样响应「检索经历正文」：只出现在正文的候选人也能经 API 召回。"""
    app, _, _ = setup_app(tmp_path)
    app.state.services.search_service = parent_and_child_service(tmp_path)
    result = await post(app, query="Kafka", mode="hybrid", search_body=True)
    assert [item["candidate_id"] for item in result["items"]] == ["c0"]


@pytest.mark.asyncio
async def test_http_ai_parse_injects_llm_conditions_and_marks_source(tmp_path):
    """开启 AI 解析：条件按 llm 来源标注，词条与语义查询回显，语义查询进入响应计划。"""
    from kerui_recruit.search.contracts import CandidateFilters
    from kerui_recruit.search.parse import ParsedPlan

    app, _, _ = setup_app(tmp_path)

    class Parser:
        async def parse(self, text, **kwargs):
            return ParsedPlan(filters=CandidateFilters(min_years=3.0, company="字节跳动"),
                              keywords="Java 字节跳动", concepts=(), conditions=(),
                              semantic_query="Java 后端", source="llm",
                              accepted_fields=("min_years", "company"))

    app.state.services.query_parser = Parser()
    plan = (await post(app, query="找 Java 后端", parse_enabled=True))["query_plan"]

    assert plan["parsed_plan_source"] == "llm"
    assert plan["keyword_terms"] == "Java 字节跳动"
    assert plan["semantic_query"] == "Java 后端"
    by_field = {item["field"]: item for item in plan["effective_conditions"]}
    assert by_field["min_years"]["source"] == "llm"
    assert by_field["company"]["value"] == "字节跳动"


@pytest.mark.asyncio
async def test_http_unmatchable_parsed_filter_degrades_to_soft_ranking(tmp_path):
    """任务组 8：AI 解析出的公司条件筛空时退化为软排，且**不被后续实时校验抵消**。

    这里同时守住一个容易踩的坑：`_hydrate_hits` 会拿 filters 再校验一遍候选人。
    如果它用的是入参而不是服务层实际生效的条件，刚被放宽掉的公司条件会被重新当成
    硬条件执行，结果又回到 0 条——「退化」静默失效，而且只在真有数据时才暴露。
    """
    from kerui_recruit.search.contracts import CandidateFilters
    from kerui_recruit.search.parse import ParsedPlan

    app, _, _ = setup_app(tmp_path)

    class Parser:
        async def parse(self, text, **kwargs):
            return ParsedPlan(filters=CandidateFilters(company="并不存在的公司"),
                              keywords="Python", concepts=(), conditions=(),
                              semantic_query=None, source="llm",
                              accepted_fields=("company",))

    app.state.services.query_parser = Parser()
    result = await post(app, query="Python", parse_enabled=True)

    assert [item["candidate_id"] for item in result["items"]] == ["c0"]
    assert result["query_plan"]["relaxed_conditions"] == ["company"]
    # 条件仍以 llm 来源回显（作为软排信号依然生效），只是不再参与过滤。
    by_field = {item["field"]: item for item in result["query_plan"]["effective_conditions"]}
    assert by_field["company"]["value"] == "并不存在的公司"


@pytest.mark.asyncio
async def test_http_panel_company_filter_is_never_relaxed(tmp_path):
    """面板手填的公司是明确意图：筛空就显示 0 条，不能被退化机制悄悄放宽。"""
    from kerui_recruit.search.contracts import CandidateFilters
    from kerui_recruit.search.parse import ParsedPlan

    app, _, _ = setup_app(tmp_path)

    class Parser:
        async def parse(self, text, **kwargs):
            return ParsedPlan(filters=CandidateFilters(company="并不存在的公司"),
                              keywords="Python", concepts=(), conditions=(),
                              semantic_query=None, source="llm",
                              accepted_fields=("company",))

    app.state.services.query_parser = Parser()
    result = await post(app, query="Python", parse_enabled=True,
                        filters={"company": "并不存在的公司"})

    assert result["items"] == []
    assert result["query_plan"]["relaxed_conditions"] == []


@pytest.mark.asyncio
async def test_http_effective_conditions_prefer_panel_values(tmp_path):
    """回显的是合并后的最终条件：同一字段面板优先，其余按规则来源标注。"""
    app, _, _ = setup_app(tmp_path)
    plan = (await post(app, query="现居上海 Java 3-5年", filters={"min_years": 8}))["query_plan"]

    by_field = {item["field"]: item for item in plan["effective_conditions"]}
    assert by_field["min_years"] == {"field": "min_years", "value": "8.0", "source": "panel",
                                     "confidence": "explicit"}
    assert by_field["locations"]["source"] == "rule"
    assert by_field["locations"]["value"] == "上海"


@pytest.mark.asyncio
async def test_http_explicit_empty_filter_removes_parsed_condition(tmp_path):
    """前端删除解析出的条件时下发显式空值：面板的「显式值优先」要能把规则解析值压掉。"""
    app, _, _ = setup_app(tmp_path)
    plan = (await post(app, query="现居上海 Java 3-5年",
                       filters={"locations": []}))["query_plan"]

    fields = {item["field"] for item in plan["effective_conditions"]}
    assert "locations" not in fields


@pytest.mark.asyncio
async def test_http_keyword_mode_accepts_ai_parse_without_semantic_channel(tmp_path):
    """关键词模式可以开 AI 解析（只取 filters + keywords），不产生语义查询、不 422。"""
    from kerui_recruit.search.contracts import CandidateFilters
    from kerui_recruit.search.parse import ParsedPlan

    app, _, _ = setup_app(tmp_path)

    class Parser:
        async def parse(self, text, **kwargs):
            return ParsedPlan(filters=CandidateFilters(min_years=3.0), keywords="Java",
                              concepts=(), conditions=(), semantic_query="Java 后端",
                              source="llm", accepted_fields=("min_years",))

    app.state.services.query_parser = Parser()
    plan = (await post(app, query="Java", mode="keyword", parse_enabled=True))["query_plan"]

    assert plan["semantic_query"] is None
    assert plan["rewrite_status"] == "not_applicable"


@pytest.mark.asyncio
async def test_query_plan_exposes_layered_rewrite_diagnostics(tmp_path):
    """公开 rewrite_status 用文档六值枚举，并附 applied / fallback_reason 诊断字段。"""
    app, _, _ = setup_app(tmp_path)
    result = await post(app, query="Python", mode="hybrid", rewrite_enabled=False)
    plan = result["query_plan"]
    assert plan["rewrite_status"] == "disabled"
    assert plan["rewrite_applied"] is False
    assert plan["rewrite_fallback_reason"] is None


class RejectingRewriter:
    async def rewrite(self, keywords):
        from kerui_recruit.search.rewrite import RewriteResult
        return RewriteResult(keywords, "rejected")


class NoopRewriter:
    async def rewrite(self, keywords):
        from kerui_recruit.search.rewrite import RewriteResult
        return RewriteResult(keywords, "unchanged")


@pytest.mark.asyncio
@pytest.mark.parametrize("rewriter,expected", [(RejectingRewriter, "rejected"), (NoopRewriter, "unchanged")])
async def test_rejected_and_unchanged_rewrites_stay_silent(tmp_path, rewriter, expected):
    """校验拒绝与无需改写都静默使用原查询，公开状态区分 rejected / unchanged。"""
    app, _, _ = setup_app(tmp_path)
    index = LanceDBSearchIndex(tmp_path / "index-r", vector_dimension=2)
    index.upsert([SearchChunk("chunk0", "c0", "r0", "Python", (1., 0.), 5, "MASTER", "上海", "AVAILABLE")])
    app.state.services.search_service = HybridSearchService(
        index=index, embedding_provider=Embedding(), reranker_provider=FakeRerankerProvider(),
        rewriter=rewriter())
    result = await post(app, query="Python", mode="hybrid", rewrite_enabled=True)
    plan = result["query_plan"]
    assert plan["rewrite_status"] == expected
    assert plan["rewrite_applied"] is False
    assert plan["rewrite_fallback_reason"] is None
    assert plan["semantic_query"] is None
