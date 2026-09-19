"""AI 复核服务 review_run 单测：反向匹配 run（多 JD）逐条取对应 JD 解析数据。"""
from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from kerui_recruit.db.migrate import migrate
from kerui_recruit.db.models import (
    Blob,
    Candidate,
    Jd,
    JdRevision,
    MatchResult,
    MatchRun,
    ResumeDocument,
    ResumeRevision,
)
from kerui_recruit.db.session import create_engine_for
from kerui_recruit.match.review import MatchReviewService, ReviewVerdictModel


class _FakeTaskClient:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def complete_json(self, messages, model, reasoning=None):
        self.prompts.append(messages[0]["content"])
        return ReviewVerdictModel(verdict="recommend", reasons=["项目匹配"], cautions=[])


class _ConditionalTaskClient:
    """按 JD 内容返回相反 verdict，用于验证反向多 JD 不串项。"""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def complete_json(self, messages, model, reasoning=None):
        prompt = messages[0]["content"]
        self.prompts.append(prompt)
        if "支付高并发服务" in prompt:
            return ReviewVerdictModel(verdict="recommend", reasons=["有支付证据"], cautions=[])
        return ReviewVerdictModel(verdict="reject", reasons=[], cautions=["无证据"])


@pytest.fixture
def factory(tmp_path: Path):
    engine = create_engine_for(tmp_path / "db.sqlite3")
    migrate(engine)
    return sessionmaker(engine, expire_on_commit=False)


def _seed_reverse_run(factory) -> str:
    with factory() as session, session.begin():
        jd1 = Jd(company="A公司", title="后端", status="OPEN")
        rev1 = JdRevision(id="jdrev-1", jd=jd1, revision_no=1, status="READY", is_current=True,
                          parsed_data={"core_duties": ["支付高并发服务"], "direction": "BACKEND"})
        jd2 = Jd(company="B公司", title="算法", status="OPEN")
        rev2 = JdRevision(id="jdrev-2", jd=jd2, revision_no=1, status="READY", is_current=True,
                          parsed_data={"core_duties": ["推荐算法"], "direction": "BACKEND"})
        blob = Blob(content_sha256="a" * 64, suffix=".pdf", size_bytes=1, storage_path="x")
        candidate = Candidate(display_name="测试", status="AVAILABLE")
        document = ResumeDocument(candidate=candidate)
        resume = ResumeRevision(id="resrev-1", document=document, blob=blob,
                                content_sha256="a" * 64, original_filename="r.pdf",
                                status="READY", is_current=True, parsed_data={"skills": ["Java"], "direction": "BACKEND"})
        session.add_all([jd1, rev1, jd2, rev2, blob, candidate, document, resume])
        session.flush()
        run = MatchRun(trigger="REVERSE_MATCH", jd_revision_id=None, mode="hybrid")
        session.add(run)
        session.flush()
        session.add(MatchResult(run=run, candidate_id=candidate.id, resume_revision_id="resrev-1",
                                jd_revision_id="jdrev-1", total_score=Decimal("0.5"), status="未处理"))
        session.add(MatchResult(run=run, candidate_id=candidate.id, resume_revision_id="resrev-1",
                                jd_revision_id="jdrev-2", total_score=Decimal("0.4"), status="未处理"))
        return run.id


@pytest.mark.asyncio
async def test_review_run_uses_per_result_jd_duties(factory):
    run_id = _seed_reverse_run(factory)
    client = _FakeTaskClient()
    service = MatchReviewService(session_factory=factory, task_client=client)

    verdicts = await service.review_run(run_id)

    assert len(verdicts) == 2
    prompt_text = "\n".join(client.prompts)
    assert "支付高并发服务" in prompt_text
    assert "推荐算法" in prompt_text
    assert all(v["verdict"] == "recommend" for v in verdicts)


@pytest.mark.asyncio
async def test_review_verdicts_carry_result_and_jd_ids(factory):
    run_id = _seed_reverse_run(factory)
    client = _ConditionalTaskClient()
    service = MatchReviewService(session_factory=factory, task_client=client)

    verdicts = await service.review_run(run_id)

    by_jd = {v["jd_revision_id"]: v for v in verdicts}
    assert by_jd["jdrev-1"]["verdict"] == "recommend"
    assert by_jd["jdrev-2"]["verdict"] == "reject"
    assert all(v["match_result_id"] for v in verdicts)
    assert all(v["candidate_id"] for v in verdicts)
    # 两个结果必须有不同的 match_result_id，避免串项。
    assert len({v["match_result_id"] for v in verdicts}) == 2


@pytest.mark.asyncio
async def test_review_run_skips_rejected_eligibility(factory):
    run_id = _seed_reverse_run(factory)
    # 把候选人方向改为与 JD 不一致（BACKEND vs ALGORITHM），使 evaluate_pair 判定 rejected。
    with factory.begin() as session:
        resume = session.get(ResumeRevision, "resrev-1")
        parsed = dict(resume.parsed_data or {})
        parsed["direction"] = "ALGORITHM"
        resume.parsed_data = parsed
    client = _FakeTaskClient()  # 即使 AI 返回 recommend，也应被资格覆盖为 pending
    service = MatchReviewService(session_factory=factory, task_client=client)

    verdicts = await service.review_run(run_id)

    assert all(v["verdict"] == "pending" for v in verdicts)
    assert client.prompts == []  # 未进入 AI 复核


def _seed_many_results(factory, count: int) -> str:
    """造一个含 count 条配对的反向 run，分数递减（便于验证按分数取前 N 条）。"""
    with factory() as session, session.begin():
        jd = Jd(company="A公司", title="后端", status="OPEN")
        rev = JdRevision(id="jdrev-many", jd=jd, revision_no=1, status="READY", is_current=True,
                         parsed_data={"core_duties": ["支付高并发服务"]})
        blob = Blob(content_sha256="b" * 64, suffix=".pdf", size_bytes=1, storage_path="y")
        candidate = Candidate(display_name="批量", status="AVAILABLE")
        document = ResumeDocument(candidate=candidate)
        resume = ResumeRevision(id="resrev-many", document=document, blob=blob,
                                content_sha256="b" * 64, original_filename="r.pdf",
                                status="READY", is_current=True, parsed_data={"skills": ["Java"]})
        session.add_all([jd, rev, blob, candidate, document, resume])
        session.flush()
        run = MatchRun(trigger="REVERSE_MATCH", jd_revision_id=None, mode="hybrid")
        session.add(run)
        session.flush()
        for index in range(count):
            session.add(MatchResult(
                run=run, candidate_id=candidate.id, resume_revision_id="resrev-many",
                jd_revision_id="jdrev-many", total_score=Decimal(str(1 - index / 100)),
                status="未处理",
            ))
        return run.id


@pytest.mark.asyncio
async def test_review_run_covers_every_matched_row(factory):
    """匹配到的每一条都要有结论：不设条数上限，25 条就复核 25 条。"""
    run_id = _seed_many_results(factory, 25)
    client = _FakeTaskClient()
    service = MatchReviewService(session_factory=factory, task_client=client)

    verdicts = await service.review_run(run_id)

    assert len(client.prompts) == 25
    assert len(verdicts) == 25
    # 每一条都对应真实配对，且没有「未复核」的汇总条。
    assert all(v["match_result_id"] for v in verdicts)
    assert not any(v.get("skipped") for v in verdicts)
    # 高分优先：第一条对应分数最高的那条配对。
    with factory() as session:
        top_id = session.scalar(
            select(MatchResult.id)
            .where(MatchResult.run_id == run_id)
            .order_by(MatchResult.total_score.desc())
            .limit(1)
        )
    assert verdicts[0]["match_result_id"] == top_id


@pytest.mark.asyncio
async def test_review_run_marks_failures_explicitly(factory):
    """单条失败必须显式可见：以前被静默写成 pending，用户只看到「没有结果」。"""
    run_id = _seed_reverse_run(factory)

    class _FailingClient:
        async def complete_json(self, messages, model, reasoning=None):
            raise RuntimeError("upstream 502")

    service = MatchReviewService(session_factory=factory, task_client=_FailingClient())

    verdicts = await service.review_run(run_id)

    assert all(v["failed"] is True for v in verdicts)
    assert all(v["error"] == "RuntimeError" for v in verdicts)
    assert all("复核失败" in v["cautions"][0] for v in verdicts)


@pytest.mark.asyncio
async def test_review_run_marks_single_call_timeout(factory, monkeypatch):
    """单条挂死不能拖住整批：超时条目单独标记，其余照常出结论。"""
    run_id = _seed_reverse_run(factory)
    monkeypatch.setattr("kerui_recruit.match.review._REVIEW_CALL_TIMEOUT_SECONDS", 0.01)

    class _HangingClient:
        async def complete_json(self, messages, model, reasoning=None):
            await asyncio.sleep(5)

    service = MatchReviewService(session_factory=factory, task_client=_HangingClient())

    verdicts = await service.review_run(run_id)

    assert all(v["failed"] is True for v in verdicts)
    assert all(v["error"] == "TimeoutError" for v in verdicts)
    assert all("复核超时" in v["cautions"][0] for v in verdicts)
