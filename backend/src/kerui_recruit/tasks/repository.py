from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session, sessionmaker

from kerui_recruit.db.models import TaskEvent, TaskRecord


class TaskLeaseError(RuntimeError):
    code = "E_TASK_LEASE"


class TaskStateError(RuntimeError):
    """任务当前状态不允许该操作（例如对进行中的任务重试）。"""

    code = "E_TASK_STATE"

    def __init__(self, message: str, *, status: str) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True, slots=True)
class TaskSpec:
    task_type: str
    queue_name: str
    priority: int
    payload: dict[str, Any]
    idempotency_key: str
    max_attempts: int = 5


@dataclass(frozen=True, slots=True)
class ClaimedTask:
    id: str
    task_type: str
    queue_name: str
    payload: dict[str, Any]
    attempts: int


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def reparse_idempotency_key(session: Session, revision_id: str, mode: str) -> str:
    """重新解析任务的幂等键（``mode`` 为 ``vision`` 或 ``ocr``）。

    进行中的同名任务保持幂等——重复点击不会重复入队；已是终态
    （``SUCCESS``/``CANCELLED``）的同名任务换一个带旧任务 id 的新键，
    否则 ``enqueue`` 会直接把旧任务还回来，「再点一次」永远不重跑。
    """
    key = f"REPARSE_RESUME:{revision_id}:{mode}"
    latest = session.scalar(
        select(TaskRecord)
        .where(TaskRecord.idempotency_key.like(key + "%"))
        .order_by(TaskRecord.created_at.desc())
        .limit(1)
    )
    if latest is None:
        return key
    if latest.status in ("SUCCESS", "CANCELLED"):
        return f"{key}:{latest.id}"
    return latest.idempotency_key


class TaskRepository:
    retry_delays = (
        timedelta(seconds=1),
        timedelta(seconds=5),
        timedelta(seconds=30),
        timedelta(minutes=2),
        timedelta(minutes=10),
    )

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        clock: Callable[[], datetime] = utc_now,
        lease_duration: timedelta = timedelta(minutes=5),
    ) -> None:
        self.session_factory = session_factory
        self.clock = clock
        self.lease_duration = lease_duration

    def enqueue(self, spec: TaskSpec) -> str:
        with self.session_factory() as session, session.begin():
            existing = session.scalar(
                select(TaskRecord).where(
                    TaskRecord.idempotency_key == spec.idempotency_key
                )
            )
            if existing is not None:
                return existing.id
            task = TaskRecord(
                task_type=spec.task_type,
                queue_name=spec.queue_name,
                priority=spec.priority,
                status="QUEUED",
                payload=spec.payload,
                idempotency_key=spec.idempotency_key,
                max_attempts=spec.max_attempts,
            )
            task.events.append(TaskEvent(from_status="PENDING", to_status="QUEUED"))
            session.add(task)
            session.flush()
            return task.id

    def claim(
        self,
        worker_id: str,
        queues: tuple[str, ...],
        *,
        now: datetime | None = None,
    ) -> ClaimedTask | None:
        moment = now or self.clock()
        with self.session_factory() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            task = session.scalar(
                select(TaskRecord)
                .where(
                    TaskRecord.queue_name.in_(queues),
                    or_(
                        TaskRecord.status.in_(("PENDING", "QUEUED")),
                        and_(
                            TaskRecord.status == "RETRY_WAIT",
                            TaskRecord.next_retry_at <= moment,
                        ),
                    ),
                )
                .order_by(TaskRecord.priority.desc(), TaskRecord.created_at.asc())
                .limit(1)
            )
            if task is None:
                session.commit()
                return None
            previous = task.status
            task.status = "RUNNING"
            task.attempts += 1
            task.lease_owner = worker_id
            task.lease_expires_at = moment + self.lease_duration
            task.next_retry_at = None
            task.events.append(TaskEvent(from_status=previous, to_status="RUNNING"))
            claimed = ClaimedTask(
                id=task.id,
                task_type=task.task_type,
                queue_name=task.queue_name,
                payload=dict(task.payload),
                attempts=task.attempts,
            )
            session.commit()
            return claimed

    def heartbeat(self, task_id: str, worker_id: str, *, progress: int | None = None) -> None:
        with self.session_factory() as session, session.begin():
            task = self._owned_running_task(session, task_id, worker_id)
            if progress is not None:
                task.progress = max(0, min(100, progress))
            task.lease_expires_at = self.clock() + self.lease_duration

    def complete(
        self,
        task_id: str,
        worker_id: str,
        *,
        result_ref: str | None = None,
    ) -> None:
        with self.session_factory() as session, session.begin():
            task = self._owned_running_task(session, task_id, worker_id)
            task.status = "SUCCESS"
            task.progress = 100
            task.result_ref = result_ref
            task.lease_owner = None
            task.lease_expires_at = None
            task.events.append(TaskEvent(from_status="RUNNING", to_status="SUCCESS"))

    def fail(
        self,
        task_id: str,
        worker_id: str,
        *,
        error_code: str,
        error_message: str,
    ) -> None:
        with self.session_factory() as session, session.begin():
            task = self._owned_running_task(session, task_id, worker_id)
            task.error_code = error_code
            task.error_message = error_message[:2_000]
            task.lease_owner = None
            task.lease_expires_at = None
            if task.attempts >= task.max_attempts:
                task.status = "DEAD_LETTER"
                task.next_retry_at = None
            else:
                task.status = "RETRY_WAIT"
                delay_index = min(task.attempts - 1, len(self.retry_delays) - 1)
                task.next_retry_at = self.clock() + self.retry_delays[delay_index]
            task.events.append(
                TaskEvent(from_status="RUNNING", to_status=task.status, message=error_code)
            )

    def recover_expired_leases(self, now: datetime | None = None) -> int:
        moment = now or self.clock()
        with self.session_factory() as session, session.begin():
            expired = session.scalars(
                select(TaskRecord).where(
                    TaskRecord.status == "RUNNING",
                    TaskRecord.lease_expires_at < moment,
                )
            ).all()
            for task in expired:
                task.status = "QUEUED"
                task.lease_owner = None
                task.lease_expires_at = None
                task.events.append(
                    TaskEvent(
                        from_status="RUNNING",
                        to_status="QUEUED",
                        message="lease_expired",
                    )
                )
            return len(expired)

    def cancel(self, task_id: str) -> None:
        """Cancel a task that has not reached a terminal state."""
        with self.session_factory() as session, session.begin():
            task = session.get(TaskRecord, task_id)
            if task is None:
                raise LookupError(f"Task not found: {task_id}")
            if task.status in ("SUCCESS", "CANCELLED", "DEAD_LETTER"):
                return
            previous = task.status
            task.status = "CANCELLED"
            task.lease_owner = None
            task.lease_expires_at = None
            task.next_retry_at = None
            task.events.append(TaskEvent(from_status=previous, to_status="CANCELLED"))

    def retry(self, task_id: str) -> None:
        """把终态任务重新排队：``FAILED`` / ``DEAD_LETTER`` / ``CANCELLED`` 都可以重试。

        状态不允许时抛 :class:`TaskStateError` 而不是静默返回——静默返回会让调用方
        「点了没反应」，前端拿不到任何反馈。
        """
        with self.session_factory() as session, session.begin():
            task = session.get(TaskRecord, task_id)
            if task is None:
                raise LookupError(f"Task not found: {task_id}")
            if task.status not in ("FAILED", "DEAD_LETTER", "CANCELLED"):
                raise TaskStateError(
                    f"任务当前状态为 {task.status}，只有失败、死信或已取消的任务可以重试",
                    status=task.status,
                )
            previous = task.status
            task.status = "QUEUED"
            task.attempts = 0
            task.error_code = None
            task.error_message = None
            task.lease_owner = None
            task.lease_expires_at = None
            task.next_retry_at = None
            task.events.append(TaskEvent(from_status=previous, to_status="QUEUED"))

    def pause(self, task_id: str) -> None:
        """Pause a task so the worker skips it until resumed.

        队列表（PENDING/QUEUED/RETRY_WAIT）与运行中的任务均可暂停；运行中任务
        被暂停后其租约失效，worker 的保活心跳会抛 TaskLeaseError 并停止处理器。
        """
        with self.session_factory() as session, session.begin():
            task = session.get(TaskRecord, task_id)
            if task is None:
                raise LookupError(f"Task not found: {task_id}")
            if task.status not in ("PENDING", "QUEUED", "RETRY_WAIT", "RUNNING"):
                return
            previous = task.status
            task.status = "PAUSED"
            task.next_retry_at = None
            task.lease_owner = None
            task.lease_expires_at = None
            task.events.append(TaskEvent(from_status=previous, to_status="PAUSED"))

    def resume(self, task_id: str) -> None:
        """Requeue a paused task."""
        with self.session_factory() as session, session.begin():
            task = session.get(TaskRecord, task_id)
            if task is None:
                raise LookupError(f"Task not found: {task_id}")
            if task.status != "PAUSED":
                return
            task.status = "QUEUED"
            task.events.append(TaskEvent(from_status="PAUSED", to_status="QUEUED"))

    def list(self, *, limit: int = 100) -> list[TaskRecord]:
        with self.session_factory() as session:
            return list(
                session.scalars(
                    select(TaskRecord)
                    .order_by(TaskRecord.created_at.desc())
                    .limit(limit)
                ).all()
            )

    def list_by_ids(self, task_ids: list[str]) -> list[TaskRecord]:
        """Return tasks by ID regardless of how recently they were created."""
        if not task_ids:
            return []
        with self.session_factory() as session:
            return list(
                session.scalars(
                    select(TaskRecord).where(TaskRecord.id.in_(task_ids))
                ).all()
            )

    @staticmethod
    def _owned_running_task(
        session: Session,
        task_id: str,
        worker_id: str,
    ) -> TaskRecord:
        task = session.get(TaskRecord, task_id)
        if task is None or task.status != "RUNNING" or task.lease_owner != worker_id:
            raise TaskLeaseError(f"Worker {worker_id} does not own task {task_id}")
        return task
