"""搜索侧 AI 复核：亮点/风险点按「查询指纹 + 候选人」落库，重复复核覆盖旧结论。"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from kerui_recruit.db.migrate import migrate
from kerui_recruit.db.models import Blob, Candidate, ResumeDocument, ResumeRevision, SearchReview
from kerui_recruit.db.session import create_engine_for
from kerui_recruit.match.review import ReviewVerdictModel
from kerui_recruit.search.review import (
    SearchReviewService,
    describe_conditions,
    query_fingerprint,
    review_base_key,
)


@pytest.fixture
def factory(tmp_path: Path):
    engine = create_engine_for(tmp_path / "db.sqlite3")
    migrate(engine)
    return sessionmaker(engine, expire_on_commit=False)


def _seed_candidates(factory, specs: dict[str, dict]) -> None:
    """按 candidate_id → parsed_data 建好当前 READY 简历版本。"""
    with factory() as session, session.begin():
        for index, (candidate_id, parsed) in enumerate(specs.items()):
            digest = str(index) * 64
            blob = Blob(content_sha256=digest, suffix=".pdf", size_bytes=1, storage_path=f"path-{index}")
            candidate = Candidate(id=candidate_id, display_name=f"候选人{index}", status="AVAILABLE")
            document = ResumeDocument(id=f"doc-{index}", candidate=candidate)
            resume = ResumeRevision(
                id=f"rev-{index}", document=document, blob=blob, content_sha256=digest,
                original_filename="r.pdf", status="READY", is_current=True, parsed_data=parsed,
            )
            session.add_all([blob, candidate, document, resume])


def _rows(factory) -> dict[str, SearchReview]:
    with factory() as session:
        return {row.candidate_id: row for row in session.scalars(select(SearchReview))}


class _SkillClient:
    """按候选人技能返回相反结论，用于验证结论按人归位。"""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def complete_json(self, messages, model, reasoning=None):
        prompt = messages[0]["content"]
        self.prompts.append(prompt)
        if "Java" in prompt:
            return ReviewVerdictModel(
                verdict="recommend", reasons=["支付经验匹配（projects[0]）"], cautions=["未见管理经验"]
            )
        return ReviewVerdictModel(verdict="reject", reasons=[], cautions=["技能不符（skills）"])


def test_query_fingerprint_is_stable_and_ignores_empty_filters():
    """空筛选与未传筛选是同一个指纹；条件变了指纹必须变。"""
    assert query_fingerprint("java", {}) == query_fingerprint(
        "java", {"candidate_status": "AVAILABLE", "locations": []}
    )
    assert query_fingerprint("java", {"min_years": 3}) != query_fingerprint("java", {"min_years": 5})


def test_describe_conditions_renders_readable_labels():
    text = describe_conditions("java 后端", {"min_years": 3, "locations": ["上海"], "candidate_status": "AVAILABLE"})

    assert "关键词：java 后端" in text
    assert "最低工作年限：3" in text
    assert "现居城市：上海" in text
    # 默认的候选人状态不算「条件」，不该出现在提示词里。
    assert "AVAILABLE" not in text


def test_review_base_key_depends_on_the_selection():
    """同一搜索条件下换一批人就是另一次复核，不能被上一条任务的幂等键挡住。"""
    assert review_base_key("qk", ["a", "b"]) == review_base_key("qk", ["b", "a"])
    assert review_base_key("qk", ["a"]) != review_base_key("qk", ["b"])


@pytest.mark.asyncio
async def test_review_query_persists_highlights_and_risks(factory):
    _seed_candidates(factory, {"cand-1": {"skills": ["Java"]}, "cand-2": {"skills": ["Go"]}})
    client = _SkillClient()
    service = SearchReviewService(session_factory=factory, task_client=client)

    summary = await service.review_query(
        query_key="qk-1", conditions="关键词：java 后端", candidate_ids=["cand-1", "cand-2"]
    )

    assert summary == {"query_key": "qk-1", "reviewed": 2, "failed": 0}
    rows = _rows(factory)
    assert rows["cand-1"].verdict == "recommend"
    assert rows["cand-1"].highlights == ["支付经验匹配（projects[0]）"]
    assert rows["cand-1"].risks == ["未见管理经验"]
    assert rows["cand-1"].failed is False
    assert rows["cand-2"].verdict == "reject"
    assert rows["cand-2"].risks == ["技能不符（skills）"]
    # 提示词里必须带上搜索条件，亮点/风险点才有依据。
    assert all("关键词：java 后端" in prompt for prompt in client.prompts)


@pytest.mark.asyncio
async def test_review_query_overwrites_previous_verdict(factory):
    """同一「查询 + 候选人」只保留一条结论：重复复核覆盖旧值而不是新增行。"""
    _seed_candidates(factory, {"cand-1": {"skills": ["Java"]}})

    class _SecondClient:
        async def complete_json(self, messages, model, reasoning=None):
            return ReviewVerdictModel(verdict="pending", reasons=["这一轮改判"], cautions=[])

    await SearchReviewService(session_factory=factory, task_client=_SkillClient()).review_query(
        query_key="qk-1", conditions="条件", candidate_ids=["cand-1"]
    )
    await SearchReviewService(session_factory=factory, task_client=_SecondClient()).review_query(
        query_key="qk-1", conditions="条件", candidate_ids=["cand-1"]
    )

    rows = _rows(factory)
    assert len(rows) == 1
    assert rows["cand-1"].verdict == "pending"
    assert rows["cand-1"].highlights == ["这一轮改判"]


@pytest.mark.asyncio
async def test_review_query_marks_missing_resume_explicitly(factory):
    """没有可用简历版本的人不能假装复核过：显式标记为待核 + 失败原因。"""
    _seed_candidates(factory, {"cand-1": {"skills": ["Java"]}})
    service = SearchReviewService(session_factory=factory, task_client=_SkillClient())

    summary = await service.review_query(query_key="qk", conditions="条件", candidate_ids=["cand-1", "ghost"])

    assert summary["reviewed"] == 2
    assert summary["failed"] == 1
    ghost = _rows(factory)["ghost"]
    assert ghost.failed is True
    assert ghost.verdict == "pending"
    assert ghost.error == "NO_PARSED_RESUME"


@pytest.mark.asyncio
async def test_review_query_marks_call_failures_explicitly(factory):
    """单条调用失败不阻断整批，但必须看得出来「没复核成功」。"""
    _seed_candidates(factory, {"cand-1": {"skills": ["Java"]}})

    class _FailingClient:
        async def complete_json(self, messages, model, reasoning=None):
            raise RuntimeError("upstream 502")

    service = SearchReviewService(session_factory=factory, task_client=_FailingClient())

    summary = await service.review_query(query_key="qk", conditions="条件", candidate_ids=["cand-1"])

    assert summary["failed"] == 1
    row = _rows(factory)["cand-1"]
    assert row.failed is True
    assert row.error == "RuntimeError"
    assert "复核失败" in row.risks[0]


@pytest.mark.asyncio
async def test_review_query_marks_single_call_timeout(factory, monkeypatch):
    """单条挂死不能拖住整批：超时条目单独标记。"""
    _seed_candidates(factory, {"cand-1": {"skills": ["Java"]}})
    monkeypatch.setattr("kerui_recruit.search.review._REVIEW_CALL_TIMEOUT_SECONDS", 0.01)

    class _HangingClient:
        async def complete_json(self, messages, model, reasoning=None):
            await asyncio.sleep(5)

    service = SearchReviewService(session_factory=factory, task_client=_HangingClient())

    await service.review_query(query_key="qk", conditions="条件", candidate_ids=["cand-1"])

    row = _rows(factory)["cand-1"]
    assert row.failed is True
    assert row.error == "TimeoutError"
    assert "复核超时" in row.risks[0]


@pytest.mark.asyncio
async def test_review_query_reports_progress_and_deduplicates(factory):
    """重复传入的候选人只复核一次；进度按去重后的总数上报。"""
    _seed_candidates(factory, {"cand-1": {"skills": ["Java"]}, "cand-2": {"skills": ["Go"]}})
    progress: list[int] = []
    service = SearchReviewService(session_factory=factory, task_client=_SkillClient())

    summary = await service.review_query(
        query_key="qk", conditions="条件",
        candidate_ids=["cand-1", "cand-1", "cand-2"], report=progress.append,
    )

    assert summary["reviewed"] == 2
    assert sorted(progress) == [50, 100]
    assert sorted(_rows(factory)) == ["cand-1", "cand-2"]
