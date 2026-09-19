"""搜索侧 AI 复核的启动/查询语义：重复点击、按人取结论、任务不存在。"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from kerui_recruit.api.errors import ApiError
from kerui_recruit.api.search import (
    SearchReviewRequest,
    get_search_review,
    start_search_review,
)
from kerui_recruit.db.migrate import migrate
from kerui_recruit.db.models import SearchReview, TaskRecord
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
        search_review_service=object(),  # 只验证入队与查询语义，不真的跑复核
    )
    engine.dispose()


def _request(services) -> SimpleNamespace:
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(services=services)))


def _tasks(services) -> list[TaskRecord]:
    with services.session_factory() as session:
        return list(session.scalars(select(TaskRecord).order_by(TaskRecord.created_at, TaskRecord.id)))


def _set_status(services, task_id: str, status: str) -> None:
    with services.session_factory() as session, session.begin():
        session.get(TaskRecord, task_id).status = status


def _command(candidate_ids=("cand-1", "cand-2"), query: str = "java 后端", reasoning: bool = False):
    return SearchReviewRequest(query=query, candidate_ids=list(candidate_ids), reasoning=reasoning)


def test_running_review_is_idempotent(services):
    """进行中的复核重复点击返回同一个任务，不会重复入队。"""
    first = start_search_review(_command(), _request(services))
    _set_status(services, first.review_id, "RUNNING")

    again = start_search_review(_command(), _request(services))

    assert again.review_id == first.review_id
    assert again.status == "RUNNING"
    assert again.query_key == first.query_key
    assert len(_tasks(services)) == 1


def test_cancelled_review_restarts_with_a_new_key(services):
    """被取消的复核要能重新开跑：换新幂等键，否则 enqueue 只会把旧任务还回来。"""
    first = start_search_review(_command(), _request(services))
    _set_status(services, first.review_id, "CANCELLED")

    restarted = start_search_review(_command(), _request(services))

    assert restarted.review_id != first.review_id
    assert restarted.status == "QUEUED"
    tasks = _tasks(services)
    assert len(tasks) == 2
    assert tasks[1].idempotency_key == f"{tasks[0].idempotency_key}:{first.review_id}"


def test_failed_review_is_requeued_in_place(services):
    """失败/死信终态原地重排：前端手里的 review_id 依然有效。"""
    first = start_search_review(_command(), _request(services))
    _set_status(services, first.review_id, "DEAD_LETTER")

    restarted = start_search_review(_command(), _request(services))

    assert restarted.review_id == first.review_id
    tasks = _tasks(services)
    assert len(tasks) == 1
    assert tasks[0].status == "QUEUED"
    assert tasks[0].attempts == 0


def test_review_task_payload_carries_conditions_and_candidates(services):
    """任务载荷要带上搜索条件与勾选人：查询接口与服务层都靠它取数据。"""
    started = start_search_review(_command(reasoning=True), _request(services))

    with services.session_factory() as session:
        task = session.get(TaskRecord, started.review_id)
    assert task.task_type == "SEARCH_REVIEW"
    assert task.payload["reasoning"] is True
    assert task.payload["candidate_ids"] == ["cand-1", "cand-2"]
    assert task.payload["query_key"] == started.query_key
    assert "关键词：java 后端" in task.payload["conditions"]
    assert task.idempotency_key.startswith(f"SEARCH_REVIEW:{started.query_key}:")


def test_different_selection_gets_a_new_task(services):
    """同一搜索条件下换一批勾选人是另一次复核，不能被上一条任务的幂等键挡住。"""
    first = start_search_review(_command(("cand-1",)), _request(services))
    second = start_search_review(_command(("cand-2",)), _request(services))

    assert second.review_id != first.review_id
    assert second.query_key == first.query_key
    assert len(_tasks(services)) == 2


def test_get_review_returns_persisted_items(services):
    """查询接口按任务载荷里的「查询 + 候选人」取已落库的结论。"""
    started = start_search_review(_command(), _request(services))
    with services.session_factory() as session, session.begin():
        session.add(SearchReview(
            query_key=started.query_key, candidate_id="cand-1", verdict="recommend",
            highlights=["Java 与支付背景匹配（projects[0]）"], risks=["未见管理经验"],
        ))
        # 不在本次勾选范围内的人不应被返回。
        session.add(SearchReview(
            query_key=started.query_key, candidate_id="cand-9", verdict="reject", highlights=[], risks=[],
        ))

    payload = get_search_review(started.review_id, _request(services))

    assert payload.status == "QUEUED"
    assert payload.progress == 0
    assert payload.query_key == started.query_key
    assert [item.candidate_id for item in payload.items] == ["cand-1"]
    assert payload.items[0].highlights == ["Java 与支付背景匹配（projects[0]）"]
    assert payload.items[0].risks == ["未见管理经验"]


def test_get_review_rejects_unknown_task(services):
    with pytest.raises(ApiError) as caught:
        get_search_review("missing", _request(services))
    assert caught.value.status_code == 404
