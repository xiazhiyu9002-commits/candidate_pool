"""主备路由：确定性目标选择、错误分类和单次故障切换。

每个业务请求最多进行两次远程生成调用（主一次、备一次）；禁止无限重试或备用失败后回到主服务。
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Protocol

from kerui_recruit.providers.ai.catalog_models import ModelProfile
from kerui_recruit.providers.ai.circuit_breaker import CircuitBreaker
from kerui_recruit.providers.ai.contracts import (
    AttemptDiagnostic,
    ExecutionContext,
    GenerationRequest,
    GenerationResult,
    ModelRole,
    ReasoningMode,
)
from kerui_recruit.providers.errors import FailureCategory, ProviderError


class TargetAdapter(Protocol):
    async def generate(self, request: GenerationRequest, *, model: str, profile: ModelProfile) -> GenerationResult: ...


@dataclass(frozen=True, slots=True)
class RouterTarget:
    connection_id: str
    provider_id: str
    role: ModelRole
    model: str
    profile: ModelProfile
    adapter: TargetAdapter
    allowed_contexts: frozenset[ExecutionContext] = frozenset(ExecutionContext)
    enabled: bool = True


def _reasoning_satisfied(request: GenerationRequest, profile: ModelProfile) -> bool:
    if request.role != ModelRole.REASONING_TEXT:
        return True
    if request.reasoning == ReasoningMode.REQUIRED:
        return ReasoningMode.REQUIRED in profile.supported_reasoning_modes or ReasoningMode.AUTO in profile.supported_reasoning_modes
    if request.reasoning == ReasoningMode.OFF:
        return ReasoningMode.OFF in profile.supported_reasoning_modes
    return True


class AiProviderRouter:
    def __init__(
        self,
        targets: list[RouterTarget],
        circuit_breaker: CircuitBreaker | None = None,
        clock=None,
    ) -> None:
        self.targets = list(targets)
        self.circuit_breaker = circuit_breaker or CircuitBreaker(clock=clock)
        self._clock = clock or time.monotonic

    def _select(self, request: GenerationRequest) -> list[RouterTarget]:
        eligible: list[RouterTarget] = []
        for target in self.targets:
            if not target.enabled:
                continue
            if target.role != request.role:
                continue
            if request.execution_context not in target.allowed_contexts:
                continue
            if not _reasoning_satisfied(request, target.profile):
                continue
            eligible.append(target)

        # 熔断冷却中的连接绝不能发出上游请求；冷却结束后的下一次合格请求作为半开探测。
        selected: list[RouterTarget] = [
            target for target in eligible
            if not self.circuit_breaker.is_open(target.connection_id, target.model)
        ]
        return selected[:2]

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        selected = self._select(request)
        if not selected:
            raise ProviderError(
                code="E_AI_NO_PROVIDER",
                retryable=False,
                user_message="没有可用的 AI 服务",
                category=FailureCategory.UNKNOWN,
                switchable=False,
            )

        attempts: list[AttemptDiagnostic] = []
        last_error: ProviderError | None = None
        for index, target in enumerate(selected):
            if request.deadline_monotonic is not None and self._clock() >= request.deadline_monotonic:
                raise ProviderError(
                    code="E_AI_DEADLINE",
                    retryable=False,
                    user_message="请求预算已耗尽",
                    category=FailureCategory.DEADLINE,
                    switchable=False,
                )
            started = self._clock()
            half_open = self.circuit_breaker.needs_half_open(target.connection_id, target.model)
            if half_open and not self.circuit_breaker.acquire_half_open(target.connection_id, target.model):
                # 半开探测被占用：记录为“跳过”而非真实失败，继续备用；
                # 避免把备用成功误记成“备用切换到备用”或使用空错误码。
                attempts.append(AttemptDiagnostic(
                    connection_id=target.connection_id,
                    provider_id=target.provider_id,
                    model=target.model,
                    error_code="E_AI_HALF_OPEN_BUSY",
                    latency_ms=0,
                ))
                continue
            try:
                result = await target.adapter.generate(request, model=target.model, profile=target.profile)
            except asyncio.CancelledError:
                if half_open:
                    self.circuit_breaker.release_half_open(target.connection_id, target.model)
                raise
            except ProviderError as error:
                attempts.append(AttemptDiagnostic(
                    connection_id=target.connection_id,
                    provider_id=target.provider_id,
                    model=target.model,
                    error_code=error.code,
                    latency_ms=int((self._clock() - started) * 1000),
                ))
                if error.switchable:
                    self.circuit_breaker.record_failure(
                        target.connection_id, error.category,
                        model=target.model,
                        retry_after_seconds=error.retry_after_seconds,
                    )
                    if half_open:
                        self.circuit_breaker.release_half_open(target.connection_id, target.model)
                    last_error = error
                    continue
                if half_open:
                    self.circuit_breaker.release_half_open(target.connection_id, target.model)
                raise

            self.circuit_breaker.record_success(target.connection_id, target.model)
            attempts.append(AttemptDiagnostic(
                connection_id=target.connection_id,
                provider_id=target.provider_id,
                model=target.model,
                error_code=None,
                latency_ms=int((self._clock() - started) * 1000),
            ))
            if index > 0:
                primary = attempts[0]
                self.circuit_breaker.record_fallback(
                    primary_provider_id=primary.provider_id,
                    primary_model=primary.model,
                    backup_provider_id=target.provider_id,
                    backup_model=target.model,
                    error_code=primary.error_code or "",
                )
            return GenerationResult(
                text=result.text,
                parsed=result.parsed,
                connection_id=target.connection_id,
                provider_id=target.provider_id,
                model=target.model,
                fallback_used=(index > 0),
                attempts=tuple(attempts),
            )

        raise ProviderError(
            code="E_AI_ALL_PROVIDERS_FAILED",
            retryable=True,
            user_message="主服务和备用服务当前均不可用",
            category=FailureCategory.UNKNOWN,
            switchable=False,
            details=tuple(
                {
                    "provider_id": item.provider_id,
                    "model": item.model,
                    "error_code": item.error_code,
                    "latency_ms": item.latency_ms,
                }
                for item in attempts
            ),
        )
