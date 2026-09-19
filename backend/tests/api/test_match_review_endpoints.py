"""AI 复核启动/查询的重复点击语义：进行中幂等、失败原地重排、终态换新幂等键。

用户现象：点「重试复核」只看到 ``{"status":"CANCELLED"}`` 两次，任务再也不跑。
根因是同键 ``enqueue`` 直接把旧任务还回来，所以这里逐状态钉住行为。
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from kerui_recruit.api.match import AiReviewStartRequest, get_ai_review, start_ai_review
from kerui_recruit.db.migrate import migrate
from kerui_recruit.db.models import TaskRecord
from kerui_recruit.db.session import create_engine_for
from kerui_recruit.tasks.repository import TaskRepository


@pytest.fixture
def services(tmp_path: Path):
    engine = create_engine_for(tmp_path / "recruit.sqlite3")
    migrate(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    yield SimpleNamespace(
        session_factory=factory,
        task_repository=TaskRepository(factory),
        match_review_service=object(),  # 只验证入队语义，不真的跑复核
    )
    engine.dispose()


def _request(services) -> SimpleNamespace:
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(services=services)))


def _tasks(services) -> list[TaskRecord]:
    with services.session_factory() as session:
        return list(session.scalars(
            select(TaskRecord).order_by(TaskRecord.created_at, TaskRecord.id)
        ))


def _set_status(services, task_id: str, status: str) -> None:
    with services.session_factory() as session, session.begin():
        session.get(TaskRecord, task_id).status = status


def test_running_review_is_idempotent(services):
    """进行中的复核重复点击返回同一个任务，不会重复入队。"""
    first = start_ai_review("run-1", _request(services), AiReviewStartRequest())
    _set_status(services, first.review_id, "RUNNING")

    again = start_ai_review("run-1", _request(services), AiReviewStartRequest())

    assert again.review_id == first.review_id
    assert again.status == "RUNNING"
    assert len(_tasks(services)) == 1


def test_cancelled_review_restarts_with_a_new_key(services):
    """被取消的复核要能重新开跑：换新幂等键，否则 enqueue 只会把旧任务还回来。"""
    first = start_ai_review("run-1", _request(services), AiReviewStartRequest())
    _set_status(services, first.review_id, "CANCELLED")

    restarted = start_ai_review("run-1", _request(services), AiReviewStartRequest())

    assert restarted.review_id != first.review_id
    assert restarted.status == "QUEUED"
    tasks = _tasks(services)
    assert len(tasks) == 2
    assert tasks[0].idempotency_key == "MATCH_REVIEW:run-1"
    assert tasks[1].idempotency_key == f"MATCH_REVIEW:run-1:{first.review_id}"
    # 新任务能被查询接口找到（按前缀取最新）。
    payload = get_ai_review("run-1", _request(services))
    assert payload["status"] == "QUEUED"


def test_succeeded_review_restarts_with_a_new_key(services):
    """已成功的复核允许「重新复核」：同样换新键，保留旧结论记录。"""
    first = start_ai_review("run-1", _request(services), AiReviewStartRequest())
    _set_status(services, first.review_id, "SUCCESS")

    restarted = start_ai_review("run-1", _request(services), AiReviewStartRequest())

    assert restarted.review_id != first.review_id
    assert len(_tasks(services)) == 2


def test_failed_review_is_requeued_in_place(services):
    """失败/死信终态原地重排：前端手里的 review_id 依然有效。"""
    first = start_ai_review("run-1", _request(services), AiReviewStartRequest())
    _set_status(services, first.review_id, "DEAD_LETTER")

    restarted = start_ai_review("run-1", _request(services), AiReviewStartRequest())

    assert restarted.review_id == first.review_id
    assert restarted.status == "QUEUED"
    tasks = _tasks(services)
    assert len(tasks) == 1
    assert tasks[0].status == "QUEUED"
    assert tasks[0].attempts == 0


def test_review_task_has_no_batch_cap(services):
    """不设条数与时长上限：复核任务的载荷里只有 run_id 与是否深度思考。"""
    started = start_ai_review("run-1", _request(services), AiReviewStartRequest(reasoning=True))

    with services.session_factory() as session:
        task = session.get(TaskRecord, started.review_id)
    assert task.payload == {"run_id": "run-1", "reasoning": True}
