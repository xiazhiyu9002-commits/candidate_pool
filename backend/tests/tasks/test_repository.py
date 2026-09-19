from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy.orm import sessionmaker

from kerui_recruit.db.migrate import migrate
from kerui_recruit.db.models import TaskRecord
from kerui_recruit.db.session import create_engine_for
from kerui_recruit.tasks.repository import TaskRepository, TaskSpec, TaskStateError


NOW = datetime(2026, 8, 29, 8, 0, tzinfo=timezone.utc)


def make_repo(tmp_path: Path) -> TaskRepository:
    engine = create_engine_for(tmp_path / "recruit.sqlite3")
    migrate(engine)
    return TaskRepository(
        sessionmaker(engine, expire_on_commit=False),
        clock=lambda: NOW,
        lease_duration=timedelta(minutes=5),
    )


def test_claim_prefers_priority_and_excludes_a_live_lease(tmp_path: Path) -> None:
    """A second worker must not execute a task whose lease is still valid."""
    repo = make_repo(tmp_path)
    low = repo.enqueue(
        TaskSpec("PARSE_RESUME", "batch", 10, {"revision_id": "low"}, "parse:low")
    )
    high = repo.enqueue(
        TaskSpec(
            "PARSE_RESUME",
            "interactive",
            100,
            {"revision_id": "high"},
            "parse:high",
        )
    )

    claimed = repo.claim("worker-1", ("interactive", "batch"))

    assert claimed is not None and claimed.id == high
    assert repo.claim("worker-2", ("interactive",)) is None
    assert repo.claim("worker-2", ("batch",)).id == low


def test_enqueue_is_idempotent(tmp_path: Path) -> None:
    """Retrying an API request must not create duplicate background work."""
    repo = make_repo(tmp_path)
    spec = TaskSpec("PARSE_RESUME", "batch", 10, {"revision_id": "one"}, "parse:one")

    assert repo.enqueue(spec) == repo.enqueue(spec)


def test_expired_running_task_returns_to_the_queue(tmp_path: Path) -> None:
    """A killed worker must not leave its task permanently stuck in RUNNING."""
    repo = make_repo(tmp_path)
    task_id = repo.enqueue(
        TaskSpec("PARSE_RESUME", "batch", 10, {"revision_id": "one"}, "parse:one")
    )
    repo.claim("dead-worker", ("batch",))

    recovered = repo.recover_expired_leases(NOW + timedelta(minutes=6))

    assert recovered == 1
    claimed = repo.claim(
        "new-worker",
        ("batch",),
        now=NOW + timedelta(minutes=6),
    )
    assert claimed is not None and claimed.id == task_id


def test_heartbeat_and_completion_persist_state(tmp_path: Path) -> None:
    """Progress and success must survive after the worker object is gone."""
    repo = make_repo(tmp_path)
    task_id = repo.enqueue(
        TaskSpec("EXPORT", "export", 20, {"format": "xlsx"}, "export:one")
    )
    repo.claim("worker-1", ("export",))

    repo.heartbeat(task_id, "worker-1", progress=60)
    repo.complete(task_id, "worker-1", result_ref="exports/result.xlsx")

    with repo.session_factory() as session:
        task = session.get(TaskRecord, task_id)
        assert task is not None
        assert task.status == "SUCCESS"
        assert task.progress == 100
        assert task.result_ref == "exports/result.xlsx"
        assert task.lease_owner is None


def test_cancel_requeued_task(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    task_id = repo.enqueue(
        TaskSpec("PARSE_RESUME", "batch", 10, {"revision_id": "one"}, "parse:one")
    )

    repo.cancel(task_id)

    with repo.session_factory() as session:
        task = session.get(TaskRecord, task_id)
        assert task.status == "CANCELLED"


def test_retry_dead_letter_task(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    task_id = repo.enqueue(
        TaskSpec("PARSE_RESUME", "batch", 10, {"revision_id": "one"}, "parse:one")
    )
    with repo.session_factory() as session, session.begin():
        session.get(TaskRecord, task_id).status = "DEAD_LETTER"

    repo.retry(task_id)

    with repo.session_factory() as session:
        task = session.get(TaskRecord, task_id)
        assert task.status == "QUEUED"
        assert task.error_code is None


def test_retry_cancelled_task(tmp_path: Path) -> None:
    """被取消的任务必须能重试：之前只认 FAILED/DEAD_LETTER，前端点重试没有任何反应。"""
    repo = make_repo(tmp_path)
    task_id = repo.enqueue(
        TaskSpec("MATCH_REVIEW", "batch", 5, {"run_id": "run-1"}, "MATCH_REVIEW:run-1")
    )
    repo.cancel(task_id)

    repo.retry(task_id)

    with repo.session_factory() as session:
        task = session.get(TaskRecord, task_id)
        assert task.status == "QUEUED"
        assert task.attempts == 0


def test_retry_non_terminal_task_raises(tmp_path: Path) -> None:
    """进行中的任务不能重试：必须明确报错而不是静默返回（否则「点了没反应」）。"""
    repo = make_repo(tmp_path)
    task_id = repo.enqueue(
        TaskSpec("PARSE_RESUME", "batch", 10, {"revision_id": "one"}, "parse:one")
    )

    with pytest.raises(TaskStateError) as excinfo:
        repo.retry(task_id)

    assert excinfo.value.status == "QUEUED"
    with repo.session_factory() as session:
        assert session.get(TaskRecord, task_id).status == "QUEUED"


def test_pause_and_resume_queued_task(tmp_path: Path) -> None:
    """A paused task must be skipped by workers until explicitly resumed."""
    repo = make_repo(tmp_path)
    task_id = repo.enqueue(
        TaskSpec("PARSE_RESUME", "batch", 10, {"revision_id": "one"}, "parse:one")
    )

    repo.pause(task_id)
    assert repo.claim("worker-1", ("batch",)) is None

    repo.resume(task_id)
    claimed = repo.claim("worker-1", ("batch",))
    assert claimed is not None and claimed.id == task_id


def test_list_returns_tasks_newest_first(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    first = repo.enqueue(
        TaskSpec("PARSE_RESUME", "batch", 10, {"revision_id": "one"}, "parse:one")
    )
    second = repo.enqueue(
        TaskSpec("PARSE_RESUME", "batch", 10, {"revision_id": "two"}, "parse:two")
    )

    tasks = repo.list()

    assert [task.id for task in tasks] == [second, first]


def test_list_by_ids_returns_tasks_beyond_recent_limit(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    ids = [
        repo.enqueue(
            TaskSpec("PARSE_RESUME", "batch", 10, {"revision_id": str(i)}, f"parse:{i}")
        )
        for i in range(250)
    ]

    # The oldest task falls outside the default 100-item list window.
    recent_ids = {task.id for task in repo.list(limit=100)}
    assert ids[0] not in recent_ids

    found = repo.list_by_ids([ids[0], ids[-1], "missing-id"])
    assert {task.id for task in found} == {ids[0], ids[-1]}
    assert repo.list_by_ids([]) == []
