"""批量操作单元测试与 API 测试：逐项结果、去重、重新解析语义。"""
from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from kerui_recruit.bulk.service import (
    _safe_name,
    bulk_delete_cases,
    bulk_force_ocr,
    bulk_reparse,
)
from kerui_recruit.core.settings import Settings
from kerui_recruit.db.models import Blob, Candidate, ResumeDocument, ResumeRevision, TaskRecord
from kerui_recruit.main import create_app
from kerui_recruit.runtime import build_runtime
from kerui_recruit.tasks.repository import reparse_idempotency_key


def test_safe_name_strips_path_and_extensions():
    assert _safe_name("a/b\\c.docx", "fallback") == "c"
    assert _safe_name(None, "fallback") == "fallback"


def test_bulk_delete_cases_deduplicates_and_reports_failures():
    services = MagicMock()
    deleted: list[str] = []

    def _delete(case_id: str) -> None:
        if case_id == "missing":
            raise LookupError("no")
        deleted.append(case_id)

    services.case_service.delete = _delete
    result = bulk_delete_cases(services, ["c1", "c1", "missing", "c2"])
    assert [r.entity_id for r in result.results] == ["c1", "missing", "c2"]
    assert result.succeeded == 2
    assert result.failed == 1
    assert result.results[1].error == "流程不存在"


def test_bulk_force_ocr_enqueues_real_ocr_payload():
    from kerui_recruit.db.models import ResumeRevision

    revision = ResumeRevision()
    revision.id = "rev-1"

    session = MagicMock()
    session.get.return_value = revision

    session_factory = MagicMock()
    session_factory.return_value.__enter__.return_value = session
    session_factory.return_value.__exit__.return_value = False

    enqueued = []
    services = MagicMock()
    services.session_factory = session_factory
    services.task_repository.enqueue = lambda spec: enqueued.append(spec) or "task-1"

    result = bulk_force_ocr(services, ["rev-1", "rev-1"])
    assert result.succeeded == 1
    assert enqueued[0].payload["force_ocr"] is True
    assert enqueued[0].payload["use_vision"] is False


def test_bulk_download_header_stays_ascii_when_items_fail():
    """批量下载的失败摘要必须能真正写进 HTTP 头。

    真机证据（2026-09-22 全量接口探针）：摘要里的中文失败原因（「无有效原件」）被直接放进
    `X-Bulk-Result`，而响应头按 latin-1 编码 → **整批下载 500**，而不是「能下的下、
    不能下的列在摘要里」；报错位置正是 uuid 之后那个冒号（`position 37`）。
    """
    from kerui_recruit.api.resumes import BulkRequest, bulk_download_endpoint

    session = MagicMock()
    session.scalar.return_value = None  # 没有可用修订 → 走中文失败分支
    session_factory = MagicMock()
    session_factory.return_value.__enter__.return_value = session
    session_factory.return_value.__exit__.return_value = False

    services = MagicMock()
    services.session_factory = session_factory
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(services=services)))

    response = bulk_download_endpoint(BulkRequest(ids=["0" * 36]), request)

    value = response.headers["X-Bulk-Result"]
    value.encode("latin-1")  # 不抛异常 = 真的能写进响应头
    assert "无有效原件" not in value
    assert "%E6%97%A0" in value  # 百分号编码后的「无」


def test_bulk_reparse_enqueues_plain_reparse_payload():
    """「重新解析」不能退化成强制 OCR：正常页仍走文本/视觉解析。"""
    revision = ResumeRevision()
    revision.id = "rev-1"

    session = MagicMock()
    session.get.return_value = revision

    session_factory = MagicMock()
    session_factory.return_value.__enter__.return_value = session
    session_factory.return_value.__exit__.return_value = False

    enqueued = []
    services = MagicMock()
    services.session_factory = session_factory
    services.task_repository.enqueue = lambda spec: enqueued.append(spec) or "task-1"

    result = bulk_reparse(services, ["rev-1"])

    assert result.succeeded == 1
    assert enqueued[0].payload["force_ocr"] is False
    assert enqueued[0].payload["use_vision"] is True


def test_reparse_idempotency_key_renews_only_after_terminal_state():
    """进行中的任务保持幂等；终态任务换新键，否则「再点一次」不会重跑。"""
    def _key_for(status: str) -> str:
        task = SimpleNamespace(id="t-9", status=status, idempotency_key="REPARSE_RESUME:rev-1:vision")
        session = MagicMock()
        session.scalar.return_value = task
        return reparse_idempotency_key(session, "rev-1", "vision")

    assert _key_for("RUNNING") == "REPARSE_RESUME:rev-1:vision"
    assert _key_for("SUCCESS") == "REPARSE_RESUME:rev-1:vision:t-9"
    assert _key_for("CANCELLED") == "REPARSE_RESUME:rev-1:vision:t-9"

    session = MagicMock()
    session.scalar.return_value = None
    assert reparse_idempotency_key(session, "rev-1", "vision") == "REPARSE_RESUME:rev-1:vision"


@pytest.fixture
def runtime(tmp_path: Path):
    rt = build_runtime(Settings(data_root=tmp_path / "data", session_token="test"))
    yield rt
    rt.services.backup_service.engine.dispose()


def _seed_ready_revision(runtime, revision_id: str) -> None:
    sha = hashlib.sha256(revision_id.encode()).hexdigest()
    with runtime.services.session_factory() as session, session.begin():
        blob = Blob(content_sha256=sha, suffix=".pdf", size_bytes=1, storage_path=f"x-{revision_id}")
        session.add(blob)
        candidate = Candidate(display_name=f"cand-{revision_id}", status="AVAILABLE")
        session.add(ResumeRevision(
            id=revision_id,
            document=ResumeDocument(candidate=candidate),
            blob=blob,
            content_sha256=sha,
            original_filename=f"{revision_id}.pdf",
            status="READY",
            is_current=True,
            parsed_data={"skills": ["Java"], "summary": "后端"},
        ))


def test_bulk_reparse_endpoint_enqueues_one_task_per_revision(runtime):
    """POST /api/resumes/bulk/reparse 逐条入队，并返回逐项结果。"""
    _seed_ready_revision(runtime, "rev-1")
    _seed_ready_revision(runtime, "rev-2")
    with TestClient(create_app(runtime.services)) as client:
        response = client.post(
            "/api/resumes/bulk/reparse",
            json={"ids": ["rev-1", "rev-2", "missing"]},
            headers={"X-Kerui-Session": "test"},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["succeeded"] == 2
    assert body["failed"] == 1
    failed = [item for item in body["results"] if not item["ok"]]
    assert failed[0]["entity_id"] == "missing"

    with runtime.services.session_factory() as session:
        tasks = session.scalars(
            select(TaskRecord).where(TaskRecord.task_type == "PARSE_RESUME")
        ).all()
        assert len(tasks) == 2
        assert all(task.payload["force_ocr"] is False for task in tasks)
        assert all(task.payload["use_vision"] is True for task in tasks)


@pytest.mark.asyncio
async def test_bulk_match_returns_rows_and_deduplicated_jd_details():
    """批量匹配要回逐人的命中岗位行；岗位详情按 JD 修订去重后单独返回。"""
    from kerui_recruit.api.match import BulkCandidateMatchRequest, bulk_match_candidates

    jd = SimpleNamespace(id="jd-1", company="示例科技", title="后端工程师", status="OPEN")
    revision = SimpleNamespace(
        id="jdrev-1", jd_id="jd-1", ai_category="NON_AI",
        parsed_data={"candidate_profile": "5 年 Java"}, source_text="JD 原文",
    )
    candidate = SimpleNamespace(display_name="张三")
    record = SimpleNamespace(
        jd_id="jd-1", revision_id="jdrev-1", company="示例科技", title="后端工程师",
        score=None, hit=SimpleNamespace(revision_id="resume-rev-1"),
    )

    session = MagicMock()
    session.scalars.return_value.all.side_effect = [[jd], [revision]]
    session.execute.return_value.all.return_value = [(revision, jd)]
    session.get.return_value = candidate

    session_factory = MagicMock()
    session_factory.return_value.__enter__.return_value = session
    session_factory.return_value.__exit__.return_value = False

    async def _reverse_match(candidate_id, *, mode="hybrid"):
        return [record]

    services = MagicMock()
    services.session_factory = session_factory
    services.match_service.reverse_match_candidate = _reverse_match
    services.match_service.record_reverse_run.return_value = SimpleNamespace(
        run_id="run-1", result_ids={"jdrev-1": "result-1"}
    )
    services.match_service.score.return_value = SimpleNamespace(total=88.5, business_match=None, breakdown={})

    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(services=services)))
    response = await bulk_match_candidates(
        BulkCandidateMatchRequest(candidate_ids=["cand-1"]), request
    )

    result = response.results[0]
    assert result.name == "张三"
    assert result.run_id == "run-1"
    assert result.matched_jobs == 1
    # result_id 是「建流程 / 标记」的凭据，必须带上
    assert result.items[0].result_id == "result-1"
    assert result.items[0].score == 88.5
    assert result.items[0].resume_revision_id == "resume-rev-1"
    assert result.items[0].parsed_data == {"candidate_profile": "5 年 Java"}
    assert [detail.revision_id for detail in response.jd_details] == ["jdrev-1"]
    assert response.jd_details[0].title == "后端工程师"
