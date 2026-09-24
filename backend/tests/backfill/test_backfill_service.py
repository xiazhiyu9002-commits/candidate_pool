from __future__ import annotations

import asyncio
import json
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy.orm import sessionmaker

from kerui_recruit.backfill.service import BackfillService
from kerui_recruit.db.migrate import migrate
from kerui_recruit.db.models import Blob, Candidate, Jd, JdRevision, ResumeDocument, ResumeRevision
from kerui_recruit.db.session import create_engine_for
from kerui_recruit.encryption.service import EncryptionService
from kerui_recruit.jd.profile import JdPointsPlan, JdProfileGenerator
from kerui_recruit.jd.structured import ExactConstraint
from kerui_recruit.providers.ai.catalog import CatalogService
from kerui_recruit.providers.ai.circuit_breaker import CircuitBreaker
from kerui_recruit.providers.ai.config_models import AiConnection, AiProviderConfig
from kerui_recruit.providers.ai.config_store import AiConfigStore
from kerui_recruit.providers.ai.contracts import ExecutionContext, ModelRole, TaskKind
from kerui_recruit.providers.ai.manager import AiProviderManager
from kerui_recruit.providers.ai.probes import AiProbeService
from kerui_recruit.providers import profile_spec
from kerui_recruit.providers.profile_pair import ProfileFact, ProfilePair, ProfilePoint
from kerui_recruit.resumes.profile import CandidateProfileGenerator
from kerui_recruit.schools.backfill import backfill_school_mappings
from kerui_recruit.schools.seed import seed_schools


class FakeLLM:
    def __init__(self, text: str = "测试画像") -> None:
        self.text = text
        self.calls = 0

    async def complete_text(self, messages, **kwargs) -> str:
        self.calls += 1
        return self.text


class RecordingLLM(FakeLLM):
    """记录每次调用传入的 messages 与 execution_context，用于断言 previous/instruction/场景已透传。"""

    def __init__(self) -> None:
        super().__init__()
        self.messages: list = []
        self.contexts: list = []

    async def complete_text(self, messages, **kwargs) -> str:
        self.messages.append(messages)
        self.contexts.append(kwargs.get("execution_context"))
        return await super().complete_text(messages, **kwargs)


class StructuredLLM(FakeLLM):
    """提供 ``complete_json`` 的生成器，用于覆盖真双形态链路（不应退化）。

    ``pair=None`` 时模拟结构化生成失败，验证退化路径仍可用。
    """

    def __init__(self, pair: ProfilePair | None = None) -> None:
        super().__init__()
        self.pair = pair
        self.json_calls = 0

    async def complete_json(self, messages, model, **kwargs) -> ProfilePair:
        self.json_calls += 1
        if self.pair is None:
            raise RuntimeError("structured generation failed")
        return self.pair


def _consistent_pair() -> ProfilePair:
    """关键事实同时出现在 narrative 与 points，满足 is_consistent。"""
    return ProfilePair(
        facts=[ProfileFact(text="交易系统", evidence_paths=["projects[0].summary"])],
        narrative="负责交易系统后端研发",
        points=[ProfilePoint(text="负责交易系统后端研发", evidence_paths=["projects[0].summary"])],
        compact="后端研发",
    )


@pytest.fixture(autouse=True)
def _skip_profile_vet(monkeypatch):
    """本文件只验证回填链路（写哪些字段、调用次数、退化路径），不验证画像口径。

    口径校验与定点重写的契约由 ``tests/providers/test_profile_pair_contract.py`` 覆盖；
    这里放行，避免测试用的短样本文本触发一次额外的定点修正调用，干扰调用次数断言。
    """
    monkeypatch.setattr(profile_spec, "vet_profile", lambda text, *, side: ())


@pytest.fixture
def factory(tmp_path):
    engine = create_engine_for(tmp_path / "db.sqlite3")
    migrate(engine)
    session_factory = sessionmaker(engine, expire_on_commit=False)
    with session_factory() as session, session.begin():
        seed_schools(session)
    yield session_factory
    engine.dispose()


def _candidate(factory, *, parsed: dict, overrides: dict | None = None) -> tuple[str, str]:
    sha = uuid4().hex + uuid4().hex
    with factory() as session, session.begin():
        candidate = Candidate(display_name="Test", status="AVAILABLE")
        revision = ResumeRevision(
            document=ResumeDocument(candidate=candidate),
            blob=Blob(content_sha256=sha, suffix=".pdf", size_bytes=1, storage_path=f"x-{uuid4().hex}"),
            content_sha256=sha, original_filename="x.pdf",
            status="READY", is_current=True, parsed_data=parsed,
            manual_overrides=overrides,
        )
        session.add(revision)
        session.flush()
        return candidate.id, revision.id


def test_school_mapping_backfill_populates_tags_and_skips_manual(factory) -> None:
    cid, _ = _candidate(factory, parsed={
        "name": "Test", "educations": [{"school": "北京大学", "degree": "硕士"}],
    })
    result = backfill_school_mappings(factory)
    assert result["updated"] == 1

    with factory() as session:
        revision = session.query(ResumeRevision).filter(
            ResumeRevision.document.has(candidate_id=cid)).one()
        edu = revision.parsed_data["educations"][0]
        assert edu["school_tags"] == ["985", "211", "双一流"]

    # 幂等：已补齐则不再更新。
    assert backfill_school_mappings(factory)["updated"] == 0

    # 人工覆盖学校字段时不改写兼容 school_level。
    cid2, _ = _candidate(factory, parsed={
        "name": "Test2", "school_level": "普通", "educations": [{"school": "清华大学"}],
    }, overrides={"school": "自定义学校"})
    backfill_school_mappings(factory)
    with factory() as session:
        revision = session.query(ResumeRevision).filter(
            ResumeRevision.document.has(candidate_id=cid2)).one()
        assert revision.parsed_data["school_level"] == "普通"


@pytest.mark.asyncio
async def test_candidate_profile_backfill_generates_and_skips_unchanged(factory) -> None:
    llm = FakeLLM()
    service = BackfillService(factory, candidate_generator=CandidateProfileGenerator(llm))
    cid, _ = _candidate(factory, parsed={"name": "Test", "skills": ["Python"]})

    result = await service.backfill_candidate_profiles()
    assert result["updated"] == 1 and result["failed"] == 0
    with factory() as session:
        revision = session.query(ResumeRevision).filter(
            ResumeRevision.document.has(candidate_id=cid)).one()
        assert revision.parsed_data["ai_profile_summary"] == "测试画像"
        assert revision.parsed_data["ai_profile_source"] == "ai"
        assert revision.parsed_data["ai_profile_input_hash"]

    # 输入未变化时再次回填不重复调用 LLM。
    assert await service.backfill_candidate_profiles() == {
        "total": 1, "updated": 0, "skipped": 1, "failed": 0, "errors": [],
    }
    assert llm.calls == 1


@pytest.mark.asyncio
async def test_candidate_profile_backfill_skips_manual_override(factory) -> None:
    llm = FakeLLM()
    service = BackfillService(factory, candidate_generator=CandidateProfileGenerator(llm))
    cid, _ = _candidate(factory, parsed={"name": "Test", "ai_profile_summary": "人工画像"},
                        overrides={"ai_profile_summary": "人工画像"})

    result = await service.backfill_candidate_profiles()
    assert result["updated"] == 0 and result["skipped"] == 1
    assert llm.calls == 0
    with factory() as session:
        revision = session.query(ResumeRevision).filter(
            ResumeRevision.document.has(candidate_id=cid)).one()
        assert revision.parsed_data["ai_profile_summary"] == "人工画像"


@pytest.mark.asyncio
async def test_candidate_backfill_fills_missing_dual_form(factory) -> None:
    """旧记录已有整体段落与正确 hash 但缺分点/叙述时，回填应补齐而非跳过。"""
    llm = FakeLLM()
    generator = CandidateProfileGenerator(llm)
    service = BackfillService(factory, candidate_generator=generator)
    data = {"name": "Test", "skills": ["Python"], "ai_profile_summary": "旧画像",
            "ai_profile_source": "ai",
            "ai_profile_input_hash": generator.input_hash({"name": "Test", "skills": ["Python"]})}
    cid, _ = _candidate(factory, parsed=data)

    result = await service.backfill_candidate_profiles()
    assert result["updated"] == 1 and result["failed"] == 0
    with factory() as session:
        revision = session.query(ResumeRevision).filter(
            ResumeRevision.document.has(candidate_id=cid)).one()
        assert revision.parsed_data["ai_profile_narrative"]
        assert revision.parsed_data["ai_profile_points"]


@pytest.mark.asyncio
async def test_jd_profile_backfill_generates(factory) -> None:
    llm = FakeLLM()
    service = BackfillService(factory, jd_generator=JdProfileGenerator(llm))
    with factory() as session, session.begin():
        jd = Jd(title="后端工程师", company="Example", status="OPEN")
        revision = JdRevision(jd=jd, source_text="后端", status="READY", is_current=True,
                              parsed_data={"title": "后端工程师", "required_skills": ["Python"]})
        session.add(revision)
        session.flush()
        jd_id = jd.id

    result = await service.backfill_jd_profiles()
    assert result["updated"] == 1 and result["failed"] == 0
    with factory() as session:
        revision = session.query(JdRevision).filter(JdRevision.jd_id == jd_id).one()
        assert revision.parsed_data["candidate_profile"] == "测试画像"
        assert revision.parsed_data["candidate_profile_source"] == "ai"
        assert revision.parsed_data["candidate_profile_input_hash"]


@pytest.mark.asyncio
async def test_jd_profile_backfill_is_idempotent(factory) -> None:
    """JD 画像回填必须按输入哈希幂等：输入未变化且画像有效时跳过 LLM。"""
    llm = FakeLLM()
    service = BackfillService(factory, jd_generator=JdProfileGenerator(llm))
    with factory() as session, session.begin():
        jd = Jd(title="后端工程师", company="Example", status="OPEN")
        revision = JdRevision(jd=jd, source_text="后端", status="READY", is_current=True,
                              parsed_data={"title": "后端工程师", "required_skills": ["Python"]})
        session.add(revision)
        session.flush()

    first = await service.backfill_jd_profiles()
    assert first["updated"] == 1
    assert llm.calls == 1

    second = await service.backfill_jd_profiles()
    assert second["updated"] == 0 and second["skipped"] == 1
    assert llm.calls == 1  # 未变化数据不重复调用 LLM


@pytest.mark.asyncio
async def test_candidate_profile_backfill_reports_incremental_progress(factory) -> None:
    """回填应逐条上报进度，最终到达 100%，而不是整批结束后才跳变。"""
    llm = FakeLLM()
    service = BackfillService(factory, candidate_generator=CandidateProfileGenerator(llm))
    for i in range(3):
        _candidate(factory, parsed={"name": f"Test{i}", "skills": ["Python"]})

    seen: list[int] = []

    result = await service.backfill_candidate_profiles(report=seen.append)

    assert result["total"] == 3 and result["updated"] == 3
    assert seen == [33, 66, 100]
    assert llm.calls == 3


@pytest.mark.asyncio
async def test_candidate_profile_backfill_survives_single_failure(factory) -> None:
    """单条生成失败不得中断整批，其余条目仍被处理。"""
    class FlakyLLM(FakeLLM):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        async def complete_text(self, messages, **kwargs) -> str:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("transient outage")
            return "测试画像"

    service = BackfillService(factory, candidate_generator=CandidateProfileGenerator(FlakyLLM()))
    for i in range(3):
        _candidate(factory, parsed={"name": f"Test{i}", "skills": ["Python"]})

    result = await service.backfill_candidate_profiles()

    assert result["total"] == 3
    assert result["failed"] == 1
    assert result["updated"] == 2


@pytest.mark.asyncio
async def test_regenerate_candidate_profile_returns_preview_without_persisting(factory) -> None:
    """重新生成仅返回预览与新 hash，不落库；保存由前端字段更新接口完成。"""
    llm = FakeLLM()
    service = BackfillService(factory, candidate_generator=CandidateProfileGenerator(llm))
    cid, _ = _candidate(factory, parsed={
        "name": "Test", "skills": ["Python"],
        "ai_profile_summary": "旧画像", "ai_profile_source": "ai",
        "ai_profile_input_hash": "old-hash", "ai_profile_stale": True,
    })

    result = await service.regenerate_candidate_profile(cid)
    assert result["generated"] is True
    assert result["summary"] == "测试画像"
    assert result["input_hash"] != "old-hash"

    with factory() as session:
        revision = session.query(ResumeRevision).filter(
            ResumeRevision.document.has(candidate_id=cid)).one()
        assert revision.parsed_data["ai_profile_summary"] == "旧画像"  # 未落库


@pytest.mark.asyncio
async def test_regenerate_candidate_profile_passes_previous_and_instruction(factory) -> None:
    """重新生成时把上一版画像与用户指令一并传入，用于增量更新。"""
    llm = RecordingLLM()
    service = BackfillService(factory, candidate_generator=CandidateProfileGenerator(llm))
    cid, _ = _candidate(factory, parsed={
        "name": "Test", "skills": ["Python"], "ai_profile_summary": "旧画像",
    })

    await service.regenerate_candidate_profile(cid, instruction="补上 RAG 经验")

    assert llm.calls == 1
    content = llm.messages[0][0]["content"]
    assert "旧画像" in content  # 上一版画像透传
    assert "补上 RAG 经验" in content  # 用户指令透传


@pytest.mark.asyncio
async def test_regenerate_jd_profile_passes_previous(factory) -> None:
    """JD 重新生成时把上一版 candidate_profile 作为 previous 传入。"""
    llm = RecordingLLM()
    service = BackfillService(factory, jd_generator=JdProfileGenerator(llm))
    with factory() as session, session.begin():
        jd = Jd(title="后端工程师", company="Example", status="OPEN")
        revision = JdRevision(jd=jd, source_text="后端", status="READY", is_current=True,
                              parsed_data={"title": "后端工程师", "required_skills": ["Python"],
                                           "candidate_profile": "旧 JD 画像"})
        session.add(revision)
        session.flush()
        jd_id = jd.id

    await service.regenerate_jd_profile(jd_id, instruction="强调高并发")

    assert llm.calls == 1
    content = llm.messages[0][0]["content"]
    assert "旧 JD 画像" in content  # 上一版画像透传
    assert "强调高并发" in content  # 用户指令透传


@pytest.mark.asyncio
async def test_candidate_backfill_persists_structured_dual_form(factory) -> None:
    """结构化生成可用时，整体/分点/浓缩必须同源落库，不做单文本拆点。"""
    llm = StructuredLLM(_consistent_pair())
    service = BackfillService(factory, candidate_generator=CandidateProfileGenerator(llm))
    cid, _ = _candidate(factory, parsed={"name": "Test", "skills": ["Python"]})

    result = await service.backfill_candidate_profiles()

    assert result["updated"] == 1 and result["failed"] == 0
    with factory() as session:
        revision = session.query(ResumeRevision).filter(
            ResumeRevision.document.has(candidate_id=cid)).one()
        assert revision.parsed_data["ai_profile_summary"] == "负责交易系统后端研发"
        assert revision.parsed_data["ai_profile_narrative"] == "负责交易系统后端研发"
        assert [p["text"] for p in revision.parsed_data["ai_profile_points"]] == ["负责交易系统后端研发"]
        assert revision.parsed_data["ai_profile_compact"] == "后端研发"
    assert llm.json_calls == 1
    assert llm.calls == 0  # 未退化为单文本生成


@pytest.mark.asyncio
async def test_candidate_backfill_falls_back_when_structured_generation_fails(factory) -> None:
    """结构化生成失败必须退化而非中断，且分点仍由单文本拆出。"""
    llm = StructuredLLM()
    service = BackfillService(factory, candidate_generator=CandidateProfileGenerator(llm))
    cid, _ = _candidate(factory, parsed={"name": "Test", "skills": ["Python"]})

    result = await service.backfill_candidate_profiles()

    assert result["updated"] == 1 and result["failed"] == 0
    with factory() as session:
        revision = session.query(ResumeRevision).filter(
            ResumeRevision.document.has(candidate_id=cid)).one()
        assert revision.parsed_data["ai_profile_summary"] == "测试画像"
        assert [p["text"] for p in revision.parsed_data["ai_profile_points"]] == ["测试画像"]
    assert llm.json_calls == 1 and llm.calls == 1


@pytest.mark.asyncio
async def test_regenerate_candidate_profile_returns_structured_dual_form(factory) -> None:
    """重新生成走结构化链路时，返回的正是真双形态预览。"""
    llm = StructuredLLM(_consistent_pair())
    service = BackfillService(factory, candidate_generator=CandidateProfileGenerator(llm))
    cid, _ = _candidate(factory, parsed={
        "name": "Test", "skills": ["Python"], "ai_profile_summary": "旧画像",
    })

    result = await service.regenerate_candidate_profile(cid)

    assert result["generated"] is True
    assert result["summary"] == "负责交易系统后端研发"
    assert [p["text"] for p in result["points"]] == ["负责交易系统后端研发"]
    assert result["compact"] == "后端研发"
    assert llm.json_calls == 1
    assert llm.calls == 0


@pytest.mark.asyncio
async def test_regenerate_candidate_profile_falls_back_when_structured_fails(factory) -> None:
    """结构化生成失败时仍返回可用预览，不向调用方抛错。"""
    llm = StructuredLLM()
    service = BackfillService(factory, candidate_generator=CandidateProfileGenerator(llm))
    cid, _ = _candidate(factory, parsed={
        "name": "Test", "skills": ["Python"], "ai_profile_summary": "旧画像",
    })

    result = await service.regenerate_candidate_profile(cid)

    assert result["summary"] == "测试画像"
    assert [p["text"] for p in result["points"]] == ["测试画像"]
    assert llm.json_calls == 1 and llm.calls == 1


@pytest.mark.asyncio
async def test_regenerate_jd_profile_returns_structured_dual_form(factory) -> None:
    """JD 重新生成走要点链路：模型只给要点，整段/分点/浓缩由要点本地派生。"""
    llm = StructuredLLM(JdPointsPlan(points=[ProfilePoint(text="负责交易系统后端研发")]))
    service = BackfillService(factory, jd_generator=JdProfileGenerator(llm))
    with factory() as session, session.begin():
        jd = Jd(title="后端工程师", company="Example", status="OPEN")
        revision = JdRevision(jd=jd, source_text="后端", status="READY", is_current=True,
                              parsed_data={"title": "后端工程师", "required_skills": ["Python"],
                                           "candidate_profile": "旧 JD 画像"})
        session.add(revision)
        session.flush()
        jd_id = jd.id

    result = await service.regenerate_jd_profile(jd_id)

    assert result["summary"] == "负责交易系统后端研发"
    assert [p["text"] for p in result["points"]] == ["负责交易系统后端研发"]
    assert result["compact"] == "负责交易系统后端研发"  # 由首个要点派生
    assert llm.calls == 0


@pytest.mark.asyncio
async def test_regenerate_jd_profile_returns_constraints_from_the_same_call(factory) -> None:
    """硬条件与画像同一次调用产出（零额外调用）；退化路径不返回该字段，前端保留既有约束。"""
    plan = JdPointsPlan(
        points=[ProfilePoint(text="负责交易系统后端研发", evidence_paths=["core_duties[0]"])],
        exact_constraints=[ExactConstraint(
            kind="skill", operator="OR", alternatives=["Java"],
            strength="MUST", source="inferred", source_text="必须熟悉 Java")],
    )
    llm = StructuredLLM(plan)
    service = BackfillService(factory, jd_generator=JdProfileGenerator(llm))
    with factory() as session, session.begin():
        jd = Jd(title="后端工程师", company="Example", status="OPEN")
        session.add(JdRevision(
            jd=jd, source_text="后端", status="READY", is_current=True,
            parsed_data={"title": "后端工程师", "required_skills": ["Python"]},
        ))
        session.flush()
        jd_id = jd.id

    result = await service.regenerate_jd_profile(jd_id)
    # 技能类的 MUST 在落库前已降级为 PLUS（不具备淘汰力，只按命中率参与排序）。
    assert result["constraints"] == [{
        "kind": "skill", "operator": "OR", "alternatives": ["Java"],
        "strength": "PLUS", "source": "inferred", "source_text": "必须熟悉 Java",
    }]

    # 结构化生成失败 → 退化为单文本，且不返回 constraints（调用方保留旧硬条件）。
    degraded = BackfillService(factory, jd_generator=JdProfileGenerator(StructuredLLM(None)))
    fallback = await degraded.regenerate_jd_profile(jd_id)
    assert "constraints" not in fallback


def _dual_manager(tmp_path, *, call_counts: dict) -> AiProviderManager:
    encryption = EncryptionService(key_path=str(tmp_path / "encryption.key"))
    catalog = CatalogService(cache_path=tmp_path / "catalog.json")
    store = AiConfigStore(path=tmp_path / "ai-providers.json", encryption=encryption, catalog_service=catalog)

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        model = body["model"]
        if "response_format" in body:
            return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok": true}'}}]})
        call_counts[model] = call_counts.get(model, 0) + 1
        return httpx.Response(200, json={"choices": [{"message": {"content": "测试画像"}}]})

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    probe = AiProbeService(catalog, http_client)
    manager = AiProviderManager(
        config_store=store, catalog_service=catalog, probe_service=probe,
        http_client=http_client, circuit_breaker=CircuitBreaker(),
    )
    manager._snapshot = manager._build_snapshot(AiProviderConfig(connections=[
        AiConnection(
            connection_id="c1", provider_id="deepseek", display_name="DeepSeek",
            api_key=SecretStr("k"), models={ModelRole.FAST_TEXT: "deepseek-v4-flash"},
            probed_roles=frozenset({ModelRole.FAST_TEXT}),
        ),
        AiConnection(
            connection_id="c2", provider_id="kimi_code", display_name="Kimi Code",
            api_key=SecretStr("k"), models={ModelRole.FAST_TEXT: "k3"},
            probed_roles=frozenset({ModelRole.FAST_TEXT}),
        ),
    ]))
    return manager


@pytest.mark.asyncio
async def test_batch_backfill_never_calls_kimi_code_second_connection(factory, tmp_path) -> None:
    call_counts: dict = {}
    manager = _dual_manager(tmp_path, call_counts=call_counts)
    generator = CandidateProfileGenerator(
        manager.task_client(TaskKind.CANDIDATE_PROFILE, ModelRole.FAST_TEXT, ExecutionContext.INTERACTIVE)
    )
    service = BackfillService(factory, candidate_generator=generator)
    _candidate(factory, parsed={"name": "Test", "skills": ["Python"]})

    result = await service.backfill_candidate_profiles()
    assert result["updated"] == 1
    assert call_counts["deepseek-v4-flash"] == 1
    assert call_counts.get("k3", 0) == 0  # 批量回填不调用 Kimi Code


class _ConcurrencyProbeLLM(FakeLLM):
    """记录同时在飞的条数与峰值：用于验证回填确实按 concurrency 并发。"""

    def __init__(self, *, delay: float = 0.05) -> None:
        super().__init__()
        self.delay = delay
        self.in_flight = 0
        self.peak = 0

    async def complete_text(self, messages, **kwargs) -> str:
        self.calls += 1
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            await asyncio.sleep(self.delay)
            return self.text
        finally:
            self.in_flight -= 1


@pytest.mark.asyncio
async def test_backfill_runs_concurrently_up_to_the_limit(factory) -> None:
    """6 条、并发 3：同时在飞正好 3，统计与串行口径一致。"""
    llm = _ConcurrencyProbeLLM()
    service = BackfillService(factory, candidate_generator=CandidateProfileGenerator(llm))
    for index in range(6):
        _candidate(factory, parsed={"name": f"C{index}", "skills": ["Python"]})

    progress: list[int] = []
    result = await service.backfill_candidate_profiles(
        concurrency=3, report=progress.append)

    assert result == {"total": 6, "updated": 6, "skipped": 0, "failed": 0, "errors": []}
    assert llm.peak == 3  # 确实并发了，且没有超过上限
    assert progress[-1] == 100


@pytest.mark.asyncio
async def test_backfill_stays_serial_when_concurrency_is_one(factory) -> None:
    llm = _ConcurrencyProbeLLM()
    service = BackfillService(factory, candidate_generator=CandidateProfileGenerator(llm))
    for index in range(3):
        _candidate(factory, parsed={"name": f"C{index}", "skills": ["Python"]})

    result = await service.backfill_candidate_profiles(concurrency=1)

    assert result["updated"] == 3
    assert llm.peak == 1


@pytest.mark.asyncio
async def test_backfill_circuit_breaker_aborts_concurrently(factory) -> None:
    """并发下熔断同样生效：不再继续领条目，未领的剩余全部计入失败。"""
    from kerui_recruit.providers.errors import FailureCategory, ProviderError

    class _NoProviderLLM(FakeLLM):
        async def complete_text(self, messages, **kwargs) -> str:
            self.calls += 1
            raise ProviderError(
                code="E_AI_NO_PROVIDER", retryable=False, user_message="没有可用的 AI 服务",
                category=FailureCategory.UNKNOWN, switchable=False,
            )

    llm = _NoProviderLLM()
    service = BackfillService(factory, candidate_generator=CandidateProfileGenerator(llm))
    for index in range(10):
        _candidate(factory, parsed={"name": f"C{index}", "skills": ["Python"]})

    result = await service.backfill_candidate_profiles(concurrency=2)

    assert result["total"] == 10  # 熔断后 remaining 补齐，进度不会停在中间
    assert result["updated"] == 0
    assert result["failed"] == 10
    assert llm.calls < 10  # 命中熔断后不再空转
    aborted = [entry for entry in result["errors"] if entry["error"] == "BatchAborted"]
    assert len(aborted) == 1 and aborted[0]["code"] == "E_AI_NO_PROVIDER"
