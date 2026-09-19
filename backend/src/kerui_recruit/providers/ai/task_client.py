"""任务感知的生成客户端外观：为业务类提供 ``complete_json`` / ``complete_text``。

业务类只声明任务角色与执行场景；本类把统一请求转发给底层 GenerationClient（路由/管理器）。
``cache_identity`` 不含密钥，配置/模型路由变化时必须变化，供语义改写缓存识别。
"""
from __future__ import annotations

from typing import Any, TypeVar

from pydantic import BaseModel

from kerui_recruit.providers.ai.contracts import (
    ExecutionContext,
    GenerationClient,
    GenerationRequest,
    ModelRole,
    OutputMode,
    ReasoningMode,
    TaskKind,
)

ResultModel = TypeVar("ResultModel", bound=BaseModel)


class TaskGenerationClient:
    def __init__(
        self,
        gateway: GenerationClient,
        *,
        task_kind: TaskKind,
        role: ModelRole,
        execution_context: ExecutionContext,
        reasoning: ReasoningMode = ReasoningMode.OFF,
    ) -> None:
        self._gateway = gateway
        self.task_kind = task_kind
        self.role = role
        self.execution_context = execution_context
        self.reasoning = reasoning

    @property
    def cache_identity(self) -> str:
        gateway_identity = getattr(self._gateway, "cache_identity", "")
        return f"{gateway_identity}|{self.task_kind.value}|{self.role.value}"

    async def complete_json(
        self,
        messages: list[dict[str, Any]],
        response_model: type[ResultModel],
        temperature: float | None = None,
        *,
        execution_context: ExecutionContext | None = None,
        deadline_monotonic: float | None = None,
        reasoning: ReasoningMode | None = None,
    ) -> ResultModel:
        request = GenerationRequest(
            messages=messages,
            role=self.role,
            task_kind=self.task_kind,
            execution_context=execution_context or self.execution_context,
            output_mode=OutputMode.JSON,
            reasoning=reasoning or self.reasoning,
            response_model=response_model,
            temperature=temperature,
            deadline_monotonic=deadline_monotonic,
        )
        result = await self._gateway.generate(request)
        if not isinstance(result.parsed, response_model):
            raise TypeError(f"gateway returned {type(result.parsed).__name__}, expected {response_model.__name__}")
        return result.parsed

    async def complete_text(
        self,
        messages: list[dict[str, Any]],
        temperature: float | None = None,
        *,
        execution_context: ExecutionContext | None = None,
        deadline_monotonic: float | None = None,
    ) -> str:
        request = GenerationRequest(
            messages=messages,
            role=self.role,
            task_kind=self.task_kind,
            execution_context=execution_context or self.execution_context,
            output_mode=OutputMode.TEXT,
            reasoning=self.reasoning,
            temperature=temperature,
            deadline_monotonic=deadline_monotonic,
        )
        result = await self._gateway.generate(request)
        return result.text
