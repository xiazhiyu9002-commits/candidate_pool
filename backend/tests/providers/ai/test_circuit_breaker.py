from __future__ import annotations

from kerui_recruit.providers.ai.circuit_breaker import CircuitBreaker
from kerui_recruit.providers.errors import FailureCategory


class Clock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_rate_limit_uses_bounded_retry_after():
    clock = Clock()
    breaker = CircuitBreaker(clock=clock)

    breaker.record_failure("c1", FailureCategory.RATE_LIMIT, retry_after_seconds=1000.0)
    # Retry-After 超出上限（300s）时封顶。
    assert breaker.is_open("c1")
    clock.advance(299)
    assert breaker.is_open("c1")
    clock.advance(2)  # 301s
    assert not breaker.is_open("c1")

    clock2 = Clock()
    breaker2 = CircuitBreaker(clock=clock2)
    breaker2.record_failure("c1", FailureCategory.RATE_LIMIT, retry_after_seconds=30.0)
    clock2.advance(31)
    assert not breaker2.is_open("c1")


def test_clear_model_unavailable_resets_model_cooldown():
    clock = Clock()
    breaker = CircuitBreaker(clock=clock)
    breaker.record_failure("c1", FailureCategory.MODEL)
    assert breaker.is_open("c1")
    breaker.clear_model_unavailable()
    assert not breaker.is_open("c1")


def test_single_half_open_probe_per_connection():
    breaker = CircuitBreaker()
    breaker.record_failure("c1", FailureCategory.SERVER)
    assert breaker.needs_half_open("c1") is False  # 冷却中，非半开
    assert breaker.acquire_half_open("c1") is True
    assert breaker.acquire_half_open("c1") is False  # 第二个探测被拒绝
    breaker.release_half_open("c1")
    assert breaker.acquire_half_open("c1") is True


def test_model_error_is_per_model_not_per_connection():
    clock = Clock()
    breaker = CircuitBreaker(clock=clock)
    breaker.record_failure("c1", FailureCategory.MODEL, model="fast-model")
    # 连接本身未熔断，同连接其他模型仍可用。
    assert breaker.is_open("c1") is False
    assert breaker.is_open("c1", model="fast-model") is True
    assert breaker.is_open("c1", model="reasoning-model") is False
    assert breaker.is_open("c1", model="vision-model") is False


def test_status_returns_connection_id_and_model_separately():
    breaker = CircuitBreaker(clock=Clock())
    breaker.record_failure("c1", FailureCategory.MODEL, model="fast-model")
    status = breaker.status()
    assert any(h.connection_id == "c1" and h.model == "fast-model" for h in status.connections)
    # 不把二者拼成一个伪连接 ID。
    assert all(":" not in h.connection_id for h in status.connections)


def test_model_success_clears_model_failure():
    clock = Clock()
    breaker = CircuitBreaker(clock=clock)
    breaker.record_failure("c1", FailureCategory.MODEL, model="fast-model")
    assert breaker.is_open("c1", model="fast-model") is True
    breaker.record_success("c1", model="fast-model")
    assert breaker.is_open("c1", model="fast-model") is False
    # 同连接其他模型不受影响。
    assert breaker.is_open("c1", model="reasoning-model") is False
