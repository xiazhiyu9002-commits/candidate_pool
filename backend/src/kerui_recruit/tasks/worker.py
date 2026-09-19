from __future__ import annotations

import asyncio
from contextlib import suppress
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from kerui_recruit.tasks.repository import TaskLeaseError, TaskRepository


TaskHandler = Callable[[dict[str, Any], Callable[[int], None] | None], Awaitable[str | None]]


def _error_code(error: Exception) -> str:
    code = getattr(error, "code", None)
    if isinstance(code, str) and code.startswith("E_"):
        return code
    return "E_TASK_HANDLER"


def _error_message(error: Exception) -> str:
    for attribute in ("user_message", "message"):
        value = getattr(error, attribute, None)
        if isinstance(value, str) and value:
            return value
    return str(error)


class TaskWorker:
    def __init__(
        self,
        *,
        repository: TaskRepository,
        worker_id: str,
        queues: tuple[str, ...],
        handlers: Mapping[str, TaskHandler],
        heartbeat_interval: float = 30.0,
    ) -> None:
        self.repository = repository
        self.worker_id = worker_id
        self.queues = queues
        self.handlers = handlers
        self.heartbeat_interval = heartbeat_interval

    async def run_once(self) -> bool:
        task = self.repository.claim(self.worker_id, self.queues)
        if task is None:
            return False
        handler = self.handlers.get(task.task_type)
        if handler is None:
            self.repository.fail(
                task.id,
                self.worker_id,
                error_code="E_TASK_HANDLER_MISSING",
                error_message=f"No handler is registered for {task.task_type}",
            )
            return True

        def _report(progress: int) -> None:
            # 处理器可在逐条处理过程中上报进度；被取消/暂停时心跳抛错，由上层循环处理。
            self.repository.heartbeat(
                task.id, self.worker_id, progress=max(0, min(100, progress))
            )

        handler_task = asyncio.create_task(handler(task.payload, _report))
        try:
            while not handler_task.done():
                done, _ = await asyncio.wait(
                    {handler_task}, timeout=self.heartbeat_interval,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if done:
                    break
                try:
                    # 保活时不覆盖处理器上报的进度。
                    await asyncio.to_thread(
                        self.repository.heartbeat, task.id, self.worker_id
                    )
                except TaskLeaseError:
                    # A user cancellation or a newer lease owns the durable
                    # state. Cooperative async handlers are stopped before they
                    # can publish their next result.
                    handler_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await handler_task
                    return True
            try:
                result_ref = await handler_task
            except TaskLeaseError:
                # 处理器在运行中通过 report 检测到取消/暂停，任务状态已由控制操作改变。
                return True
            except Exception as error:
                try:
                    self.repository.fail(
                        task.id,
                        self.worker_id,
                        error_code=_error_code(error),
                        error_message=_error_message(error),
                    )
                except TaskLeaseError:
                    pass
            else:
                try:
                    self.repository.complete(
                        task.id,
                        self.worker_id,
                        result_ref=result_ref,
                    )
                except TaskLeaseError:
                    # Cancellation won the commit fence.
                    pass
        except asyncio.CancelledError:
            handler_task.cancel()
            with suppress(asyncio.CancelledError):
                await handler_task
            raise
        return True
