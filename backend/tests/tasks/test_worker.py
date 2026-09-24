from datetime import datetime, timedelta, timezone
import asyncio
from pathlib import Path

import pytest
from sqlalchemy.orm import sessionmaker

from kerui_recruit.db.migrate import migrate
from kerui_recruit.db.models import TaskRecord
from kerui_recruit.db.session import create_engine_for
from kerui_recruit.tasks.repository import TaskRepository, TaskSpec
from kerui_recruit.tasks.worker import TaskWorker


@pytest.mark.asyncio
async def test_worker_executes_registered_handler_and_completes_task(tmp_path: Path) -> None:
    """A claimed task must reach SUCCESS with the handler's durable result reference."""
    engine = create_engine_for(tmp_path / "recruit.sqlite3")
    migrate(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    repo = TaskRepository(
        factory,
        clock=lambda: datetime(2026, 8, 29, tzinfo=timezone.utc),
        lease_duration=timedelta(minutes=5),
    )
    task_id = repo.enqueue(
        TaskSpec("EXPORT", "export", 10, {"format": "xlsx"}, "export:one")
    )

    async def export_handler(payload: dict[str, str], report=None) -> str:
        assert payload == {"format": "xlsx"}
        return "exports/result.xlsx"

    worker = TaskWorker(
        repository=repo,
        worker_id="worker-1",
        queues=("export",),
        handlers={"EXPORT": export_handler},
    )

    assert await worker.run_once() is True
    assert await worker.run_once() is False
    with factory() as session:
        task = session.get(TaskRecord, task_id)
        assert task is not None
        assert task.status == "SUCCESS"
        assert task.result_ref == "exports/result.xlsx"


@pytest.mark.asyncio
async def test_worker_failure_schedules_a_durable_retry(tmp_path: Path) -> None:
    """A transient handler crash must release its lease and remain retryable."""
    engine = create_engine_for(tmp_path / "recruit.sqlite3")
    migrate(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    repo = TaskRepository(
        factory,
        clock=lambda: datetime(2026, 8, 29, tzinfo=timezone.utc),
        lease_duration=timedelta(minutes=5),
    )
    task_id = repo.enqueue(
        TaskSpec("EXPORT", "export", 10, {"format": "xlsx"}, "export:failure")
    )

    async def failing_handler(payload: dict[str, str], report=None) -> str:
        raise RuntimeError(f"cannot export {payload['format']}")

    worker = TaskWorker(
        repository=repo,
        worker_id="worker-1",
        queues=("export",),
        handlers={"EXPORT": failing_handler},
    )

    assert await worker.run_once() is True
    with factory() as session:
        task = session.get(TaskRecord, task_id)
        assert task is not None
        assert task.status == "RETRY_WAIT"
        assert task.error_code == "E_TASK_HANDLER"
        assert task.next_retry_at is not None
        assert task.lease_owner is None


@pytest.mark.asyncio
async def test_content_level_failure_is_not_retried(tmp_path: Path) -> None:
    """内容级判定（`E_PARSE_INCOMPLETE`）必须**直接进终态**，不再重试。

    重试的前提是「换个时机可能就好了」。而「这份原文抽不出够用的字段」换多少次时机都一样，
    只会把同一次模型调用再烧 4 遍并占住 worker；实测 `.dev-data` 里就有 attempts=2
    还挂在 RETRY_WAIT 的 E_PARSE_INCOMPLETE。
    """
    engine = create_engine_for(tmp_path / "recruit.sqlite3")
    migrate(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    repo = TaskRepository(factory, lease_duration=timedelta(minutes=5))
    task_id = repo.enqueue(TaskSpec("PARSE", "normal", 10, {}, "parse:one"))

    class _Incomplete(RuntimeError):
        code = "E_PARSE_INCOMPLETE"
        user_message = "解析结果不完整，已归为不合格，可重新解析"

    async def handler(payload, report=None):
        raise _Incomplete("incomplete")

    worker = TaskWorker(repository=repo, worker_id="worker", queues=("normal",),
                        handlers={"PARSE": handler})

    assert await worker.run_once() is True
    with factory() as session:
        task = session.get(TaskRecord, task_id)
        assert task.status == "DEAD_LETTER"
        assert task.next_retry_at is None
        assert task.error_code == "E_PARSE_INCOMPLETE"


@pytest.mark.asyncio
async def test_provider_outage_is_still_retried(tmp_path: Path) -> None:
    """供应商侧错误仍要重试 —— 上面那条收的是确定性判定，不是把所有失败都变成终态。"""
    engine = create_engine_for(tmp_path / "recruit.sqlite3")
    migrate(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    repo = TaskRepository(factory, lease_duration=timedelta(minutes=5))
    task_id = repo.enqueue(TaskSpec("PARSE", "normal", 10, {}, "parse:two"))

    class _Outage(RuntimeError):
        code = "E_AI_ALL_PROVIDERS_FAILED"
        user_message = "主服务和备用服务当前均不可用"

    async def handler(payload, report=None):
        raise _Outage("outage")

    worker = TaskWorker(repository=repo, worker_id="worker", queues=("normal",),
                        handlers={"PARSE": handler})

    assert await worker.run_once() is True
    with factory() as session:
        task = session.get(TaskRecord, task_id)
        assert task.status == "RETRY_WAIT"
        assert task.next_retry_at is not None


@pytest.mark.asyncio
async def test_running_cancel_stops_handler_without_killing_worker(tmp_path: Path) -> None:
    engine = create_engine_for(tmp_path / "recruit.sqlite3")
    migrate(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    repo = TaskRepository(factory, lease_duration=timedelta(seconds=1))
    task_id = repo.enqueue(TaskSpec("WAIT", "normal", 10, {}, "wait:one"))
    started = asyncio.Event()
    allow_write = asyncio.Event()
    writes: list[str] = []

    async def handler(payload, report=None):
        started.set()
        await allow_write.wait()
        writes.append("published")

    worker = TaskWorker(repository=repo, worker_id="worker", queues=("normal",),
                        handlers={"WAIT": handler}, heartbeat_interval=0.01)
    running = asyncio.create_task(worker.run_once())
    await started.wait()
    repo.cancel(task_id)
    assert await asyncio.wait_for(running, 0.5) is True
    assert writes == []
    assert repo.list()[0].status == "CANCELLED"

    second = repo.enqueue(TaskSpec("WAIT", "normal", 10, {}, "wait:two"))
    allow_write.set()
    assert await worker.run_once() is True
    with factory() as session:
        assert session.get(TaskRecord, second).status == "SUCCESS"


@pytest.mark.asyncio
async def test_worker_persists_handler_progress_and_keepalive_preserves_it(tmp_path: Path) -> None:
    """处理器上报的进度必须被持久化，且保活心跳不得把进度清零。"""
    engine = create_engine_for(tmp_path / "recruit.sqlite3")
    migrate(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    repo = TaskRepository(factory, lease_duration=timedelta(seconds=1))
    task_id = repo.enqueue(TaskSpec("PROGRESS", "normal", 10, {}, "progress:one"))
    reported = asyncio.Event()
    proceed = asyncio.Event()

    async def handler(payload, report=None):
        report(42)
        reported.set()
        await proceed.wait()

    worker = TaskWorker(repository=repo, worker_id="worker", queues=("normal",),
                        handlers={"PROGRESS": handler}, heartbeat_interval=0.01)
    running = asyncio.create_task(worker.run_once())
    await reported.wait()
    # 让保活心跳至少执行一次，确认它不会把已上报的 42 覆盖为 0。
    await asyncio.sleep(0.05)
    with factory() as session:
        assert session.get(TaskRecord, task_id).progress == 42
    proceed.set()
    assert await asyncio.wait_for(running, 0.5) is True


@pytest.mark.asyncio
async def test_pause_running_task_stops_handler(tmp_path: Path) -> None:
    """运行中任务被暂停后，处理器应停止且任务进入 PAUSED，不写结果。"""
    engine = create_engine_for(tmp_path / "recruit.sqlite3")
    migrate(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    repo = TaskRepository(factory, lease_duration=timedelta(seconds=1))
    task_id = repo.enqueue(TaskSpec("WAIT", "normal", 10, {}, "wait:pause"))
    started = asyncio.Event()
    writes: list[str] = []

    async def handler(payload, report=None):
        started.set()
        await asyncio.sleep(10)
        writes.append("published")

    worker = TaskWorker(repository=repo, worker_id="worker", queues=("normal",),
                        handlers={"WAIT": handler}, heartbeat_interval=0.01)
    running = asyncio.create_task(worker.run_once())
    await started.wait()
    repo.pause(task_id)
    assert await asyncio.wait_for(running, 0.5) is True
    assert writes == []
    assert repo.list()[0].status == "PAUSED"
