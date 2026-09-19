"""内存态熔断与健康状态：连接/模型冷却、自动回切、半开探测并发保护。

- 连接级错误（网络/限流/鉴权/额度）按 connection_id 冷却。
- 模型级错误（下架/无权限/能力不支持）按 connection_id + model 冷却，不影响同连接其他模型。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from kerui_recruit.providers.errors import FailureCategory

_RATE_LIMIT_COOLDOWN = 60.0
_RATE_LIMIT_MAX = 300.0
_NETWORK_COOLDOWN = 30.0
_NETWORK_REPEATED = 120.0
_AUTH_COOLDOWN = 900.0
_QUOTA_COOLDOWN = 300.0
_MODEL_COOLDOWN = 3600.0
_CONSECUTIVE_THRESHOLD = 3


def cooldown_seconds(category: FailureCategory, consecutive: int = 0) -> float:
    if category == FailureCategory.RATE_LIMIT:
        return _RATE_LIMIT_COOLDOWN
    if category == FailureCategory.AUTH:
        return _AUTH_COOLDOWN
    if category == FailureCategory.QUOTA:
        return _QUOTA_COOLDOWN
    if category == FailureCategory.MODEL:
        return _MODEL_COOLDOWN
    if consecutive >= _CONSECUTIVE_THRESHOLD:
        return _NETWORK_REPEATED
    return _NETWORK_COOLDOWN


def _key(connection_id: str, model: str | None) -> str:
    return f"{connection_id}:{model}" if model else connection_id


@dataclass
class _Health:
    connection_id: str = ""
    model: str | None = None
    cooldown_until: float = 0.0
    consecutive_failures: int = 0
    last_error_code: str | None = None
    half_open_in_progress: bool = False


@dataclass(frozen=True, slots=True)
class ConnectionHealth:
    connection_id: str
    model: str | None
    circuit_state: str  # "closed" | "open" | "half_open"
    consecutive_failures: int
    last_error_code: str | None
    cooldown_until: float


@dataclass(frozen=True, slots=True)
class FallbackSummary:
    """脱敏切换摘要：主/备供应商与模型，以及触发切换的错误类别。"""
    primary_provider_id: str
    primary_model: str
    backup_provider_id: str
    backup_model: str
    error_code: str
    at_monotonic: float
    occurred_at: float


@dataclass(frozen=True, slots=True)
class RouterStatus:
    connections: tuple[ConnectionHealth, ...] = field(default_factory=tuple)
    last_fallback: FallbackSummary | None = None


class CircuitBreaker:
    def __init__(self, clock: object = None) -> None:
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._health: dict[str, _Health] = {}
        self._last_fallback: FallbackSummary | None = None

    def is_open(self, connection_id: str, model: str | None = None) -> bool:
        keys = [connection_id]
        if model is not None:
            keys.append(_key(connection_id, model))
        with self._lock:
            for key in keys:
                health = self._health.get(key)
                if health is not None and health.cooldown_until > self._clock():
                    return True
            return False

    def record_failure(self, connection_id: str, category: FailureCategory, model: str | None = None, retry_after_seconds: float | None = None) -> None:
        # MODEL 类错误按 connection_id + model 冷却，避免快速模型下架连带思考/视觉模型一起冷却。
        is_model = category == FailureCategory.MODEL and model is not None
        key = _key(connection_id, model) if is_model else connection_id
        with self._lock:
            health = self._health.setdefault(key, _Health(connection_id=connection_id, model=model if is_model else None))
            health.consecutive_failures += 1
            health.last_error_code = category.value
            cooldown = cooldown_seconds(category, health.consecutive_failures)
            if category == FailureCategory.RATE_LIMIT and retry_after_seconds is not None:
                cooldown = min(max(retry_after_seconds, 0.0), _RATE_LIMIT_MAX)
            health.cooldown_until = self._clock() + cooldown

    def clear_model_unavailable(self) -> None:
        """清除因「模型不存在/下架/无权限」产生的冷却；配置更新或探测成功后调用。"""
        with self._lock:
            for health in self._health.values():
                if health.last_error_code == FailureCategory.MODEL.value:
                    health.last_error_code = None
                    health.consecutive_failures = 0
                    health.cooldown_until = 0.0
                    health.half_open_in_progress = False

    def record_success(self, connection_id: str, model: str | None = None) -> None:
        with self._lock:
            # 清除连接级状态。
            conn_health = self._health.setdefault(connection_id, _Health(connection_id=connection_id))
            conn_health.consecutive_failures = 0
            conn_health.last_error_code = None
            conn_health.cooldown_until = 0.0
            conn_health.half_open_in_progress = False
            # 清除对应模型级状态（模型调用成功后清除该模型的失败计数与错误状态）。
            if model is not None:
                mhealth = self._health.get(_key(connection_id, model))
                if mhealth is not None:
                    mhealth.consecutive_failures = 0
                    mhealth.last_error_code = None
                    mhealth.cooldown_until = 0.0
                    mhealth.half_open_in_progress = False

    def _candidate_keys(self, connection_id: str, model: str | None) -> list[str]:
        keys = [connection_id]
        if model is not None:
            keys.append(_key(connection_id, model))
        return keys

    def acquire_half_open(self, connection_id: str, model: str | None = None) -> bool:
        """同一连接（或连接+模型）同一时刻最多一个半开探测；返回是否获得探测权。"""
        with self._lock:
            # 优先在已有失败记录的 key 上获取半开权。
            for key in self._candidate_keys(connection_id, model):
                health = self._health.get(key)
                if health is not None and health.consecutive_failures > 0:
                    if health.half_open_in_progress:
                        return False
                    health.half_open_in_progress = True
                    return True
            key = _key(connection_id, model)
            health = self._health.setdefault(key, _Health(connection_id=connection_id, model=model))
            health.half_open_in_progress = True
            return True

    def needs_half_open(self, connection_id: str, model: str | None = None) -> bool:
        """冷却已结束但仍曾失败：下一次请求应作为半开探测。"""
        with self._lock:
            for key in self._candidate_keys(connection_id, model):
                health = self._health.get(key)
                if health is not None and health.consecutive_failures > 0 and health.cooldown_until <= self._clock():
                    return True
            return False

    def release_half_open(self, connection_id: str, model: str | None = None) -> None:
        with self._lock:
            for key in self._candidate_keys(connection_id, model):
                health = self._health.get(key)
                if health is not None and health.half_open_in_progress:
                    health.half_open_in_progress = False

    def record_fallback(
        self,
        *,
        primary_provider_id: str,
        primary_model: str,
        backup_provider_id: str,
        backup_model: str,
        error_code: str,
    ) -> None:
        with self._lock:
            self._last_fallback = FallbackSummary(
                primary_provider_id=primary_provider_id,
                primary_model=primary_model,
                backup_provider_id=backup_provider_id,
                backup_model=backup_model,
                error_code=error_code,
                at_monotonic=self._clock(),
                occurred_at=time.time(),
            )

    def status(self) -> RouterStatus:
        with self._lock:
            rows = []
            for health in self._health.values():
                if health.cooldown_until > self._clock():
                    state = "open"
                elif health.half_open_in_progress:
                    state = "half_open"
                else:
                    state = "closed"
                rows.append(ConnectionHealth(
                    connection_id=health.connection_id,
                    model=health.model,
                    circuit_state=state,
                    consecutive_failures=health.consecutive_failures,
                    last_error_code=health.last_error_code,
                    cooldown_until=health.cooldown_until,
                ))
            return RouterStatus(connections=tuple(rows), last_fallback=self._last_fallback)
