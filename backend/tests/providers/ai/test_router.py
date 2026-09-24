from __future__ import annotations

import asyncio

import pytest

from kerui_recruit.providers.ai.catalog_models import ModelProfile
from kerui_recruit.providers.ai.circuit_breaker import CircuitBreaker
from kerui_recruit.providers.ai.contracts import (
    ExecutionContext,
    GenerationRequest,
    GenerationResult,
    ModelRole,
    OutputMode,
    ReasoningMode,
    TaskKind,
)
from kerui_recruit.providers.ai.router import AiProviderRouter, RouterTarget
from kerui_recruit.providers.errors import FailureCategory, ProviderError


class FakeClock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def profile(**kwargs) -> ModelProfile:
    defaults = dict(
        model_id="model",
        roles=frozenset({ModelRole.FAST_TEXT}),
        supported_reasoning_modes=frozenset({ReasoningMode.OFF, ReasoningMode.REQUIRED}),
    )
    defaults.update(kwargs)
    return ModelProfile(**defaults)


def request(**kwargs) -> GenerationRequest:
    defaults = dict(
        messages=[{"role": "user", "content": "hi"}],
        role=ModelRole.FAST_TEXT,
        task_kind=TaskKind.QUERY_REWRITE,
        execution_context=ExecutionContext.INTERACTIVE,
        output_mode=OutputMode.TEXT,
        reasoning=ReasoningMode.OFF,
    )
    defaults.update(kwargs)
    return GenerationRequest(**defaults)


def error(code: str, category: FailureCategory, switchable: bool) -> ProviderError:
    return ProviderError(code=code, retryable=True, user_message=code, category=category, switchable=switchable)


class MockAdapter:
    def __init__(self, *, result: str = None, error: ProviderError = None, on_call=None) -> None:
        self.calls = 0
        self.result = result
        self.error = error
        self.on_call = on_call

    async def generate(self, req, *, model, profile):
        self.calls += 1
        if self.on_call is not None:
            self.on_call()
        if self.error is not None:
            raise self.error
        return GenerationResult(text=self.result, parsed=None, connection_id="", provider_id="", model=model)


class CancelAwareAdapter:
    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def generate(self, req, *, model, profile):
        self.started.set()
        await asyncio.sleep(10)


def target(provider_id: str, adapter, *, role: ModelRole = ModelRole.FAST_TEXT, connection_id: str | None = None, allowed_contexts=None, profile_kwargs=None, reasoning_effort=None) -> RouterTarget:
    kwargs = dict(profile_kwargs or {})
    kwargs.setdefault("roles", frozenset({role}))
    return RouterTarget(
        connection_id=connection_id or provider_id,
        provider_id=provider_id,
        role=role,
        model="model",
        profile=profile(**kwargs),
        adapter=adapter,
        allowed_contexts=allowed_contexts if allowed_contexts is not None else frozenset(ExecutionContext),
        reasoning_effort=reasoning_effort,
    )


@pytest.mark.asyncio
async def test_429_switches_once_to_secondary():
    primary = MockAdapter(error=error("E_API_RATE_LIMIT", FailureCategory.RATE_LIMIT, True))
    secondary = MockAdapter(result="ok")
    route = AiProviderRouter([target("deepseek", primary), target("qwen", secondary)])
    result = await route.generate(request())
    assert result.text == "ok"
    assert result.fallback_used is True
    assert [item.provider_id for item in result.attempts] == ["deepseek", "qwen"]
    assert primary.calls == 1 and secondary.calls == 1


@pytest.mark.asyncio
async def test_successful_fallback_records_sanitized_summary():
    primary = MockAdapter(error=error("E_API_RATE_LIMIT", FailureCategory.RATE_LIMIT, True))
    secondary = MockAdapter(result="ok")
    route = AiProviderRouter([target("deepseek", primary), target("qwen", secondary)])
    await route.generate(request())
    summary = route.circuit_breaker.status().last_fallback
    assert summary is not None
    assert summary.primary_provider_id == "deepseek"
    assert summary.backup_provider_id == "qwen"
    assert summary.error_code == "E_API_RATE_LIMIT"


@pytest.mark.asyncio
async def test_no_fallback_leaves_summary_untouched():
    primary = MockAdapter(result="primary-ok")
    secondary = MockAdapter(result="backup")
    route = AiProviderRouter([target("deepseek", primary), target("qwen", secondary)])
    await route.generate(request())
    assert route.circuit_breaker.status().last_fallback is None


@pytest.mark.asyncio
async def test_input_error_never_sends_data_to_secondary():
    primary = MockAdapter(error=error("E_API_INPUT", FailureCategory.INPUT, False))
    secondary = MockAdapter(result="should not run")
    route = AiProviderRouter([target("deepseek", primary), target("qwen", secondary)])
    with pytest.raises(ProviderError):
        await route.generate(request())
    assert secondary.calls == 0


@pytest.mark.asyncio
async def test_auth_and_quota_failures_use_authorized_backup():
    for category in (FailureCategory.AUTH, FailureCategory.QUOTA):
        primary = MockAdapter(error=error("E", category, True))
        secondary = MockAdapter(result="ok")
        result = await AiProviderRouter([target("deepseek", primary), target("qwen", secondary)]).generate(request())
        assert result.fallback_used is True


@pytest.mark.asyncio
async def test_kimi_code_is_filtered_from_background_and_batch():
    primary = MockAdapter(error=error("E_API_BUSY", FailureCategory.SERVER, True))
    kimi = MockAdapter(result="ok")
    route = AiProviderRouter([
        target("deepseek", primary),
        target("kimi_code", kimi, allowed_contexts=frozenset({ExecutionContext.INTERACTIVE})),
    ])
    with pytest.raises(ProviderError):
        await route.generate(request(execution_context=ExecutionContext.BACKGROUND))
    assert kimi.calls == 0


@pytest.mark.asyncio
async def test_only_the_top_two_ordered_targets_are_attempted():
    first = MockAdapter(error=error("E_API_BUSY", FailureCategory.SERVER, True))
    second = MockAdapter(error=error("E_API_BUSY", FailureCategory.SERVER, True))
    third = MockAdapter(result="third")
    route = AiProviderRouter([
        target("deepseek", first),
        target("qwen", second),
        target("zhipu", third),
    ])
    with pytest.raises(ProviderError):
        await route.generate(request())
    # 列表顺序即主备顺序：第三个连接是候补，主备都失败也不越位调用。
    assert first.calls == 1 and second.calls == 1
    assert third.calls == 0


@pytest.mark.asyncio
async def test_third_connection_is_promoted_once_the_first_two_are_cooling():
    clock = FakeClock()
    first = MockAdapter(error=error("E_API_BUSY", FailureCategory.SERVER, True))
    second = MockAdapter(error=error("E_API_BUSY", FailureCategory.SERVER, True))
    third = MockAdapter(result="third")
    route = AiProviderRouter([
        target("deepseek", first),
        target("qwen", second),
        target("zhipu", third),
    ], clock=clock)
    with pytest.raises(ProviderError):
        await route.generate(request())
    # 前两个进入冷却后，候补顺位前移成为唯一可用目标。
    result = await route.generate(request())
    assert result.provider_id == "zhipu"
    assert third.calls == 1


@pytest.mark.asyncio
async def test_primary_is_retried_after_cooldown_and_restored():
    clock = FakeClock()
    flaky = MockAdapter(error=error("E_API_RATE_LIMIT", FailureCategory.RATE_LIMIT, True))
    backup = MockAdapter(result="backup")
    route = AiProviderRouter([target("deepseek", flaky), target("qwen", backup)], clock=clock)
    assert (await route.generate(request())).provider_id == "qwen"
    # 冷却结束后，主服务半开恢复。
    flaky.error = None
    flaky.result = "primary-ok"
    clock.advance(61)
    assert (await route.generate(request())).provider_id == "deepseek"


@pytest.mark.asyncio
async def test_cancelled_primary_never_calls_secondary():
    primary = CancelAwareAdapter()
    secondary = MockAdapter(result="must not run")
    route = AiProviderRouter([target("deepseek", primary), target("qwen", secondary)])
    task = asyncio.create_task(route.generate(request()))
    await primary.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert secondary.calls == 0


@pytest.mark.asyncio
async def test_exhausted_shared_deadline_never_starts_secondary():
    clock = FakeClock(now=0.0)
    primary = MockAdapter(
        error=error("E_API_BUSY", FailureCategory.SERVER, True),
        on_call=lambda: clock.advance(10.0),
    )
    secondary = MockAdapter(result="must not run")
    route = AiProviderRouter([target("deepseek", primary), target("qwen", secondary)], clock=clock)
    with pytest.raises(ProviderError) as caught:
        await route.generate(request(deadline_monotonic=5.0))
    assert caught.value.code == "E_AI_DEADLINE"
    assert secondary.calls == 0


@pytest.mark.asyncio
async def test_both_fail_raises_all_providers_failed():
    primary = MockAdapter(error=error("E_API_BUSY", FailureCategory.SERVER, True))
    secondary = MockAdapter(error=error("E_API_BUSY", FailureCategory.SERVER, True))
    route = AiProviderRouter([target("deepseek", primary), target("qwen", secondary)])
    with pytest.raises(ProviderError) as caught:
        await route.generate(request())
    assert caught.value.code == "E_AI_ALL_PROVIDERS_FAILED"
    assert primary.calls == 1 and secondary.calls == 1
    # 脱敏 attempts：只含供应商、模型、错误码、耗时，不含 connection_id/正文/密钥。
    assert caught.value.details == (
        {"provider_id": "deepseek", "model": "model", "error_code": "E_API_BUSY", "latency_ms": 0},
        {"provider_id": "qwen", "model": "model", "error_code": "E_API_BUSY", "latency_ms": 0},
    )


@pytest.mark.asyncio
async def test_all_connections_cooling_returns_error_without_calls():
    clock = FakeClock()
    primary = MockAdapter(error=error("E_API_RATE_LIMIT", FailureCategory.RATE_LIMIT, True))
    backup = MockAdapter(error=error("E_API_RATE_LIMIT", FailureCategory.RATE_LIMIT, True))
    route = AiProviderRouter([target("deepseek", primary), target("qwen", backup)], clock=clock)
    # 第一次：主 429 → 备 429，两者都进入冷却。
    with pytest.raises(ProviderError):
        await route.generate(request())
    assert primary.calls == 1 and backup.calls == 1
    # 第二次（冷却中）：不得发出任何上游请求，快速返回安全错误。
    with pytest.raises(ProviderError) as caught:
        await route.generate(request())
    assert caught.value.code == "E_AI_NO_PROVIDER"
    assert primary.calls == 1 and backup.calls == 1  # 远程调用数为 0


@pytest.mark.asyncio
async def test_half_open_concurrency_records_correct_fallback_not_fake():
    clock = FakeClock()
    primary = MockAdapter(error=error("E_API_RATE_LIMIT", FailureCategory.RATE_LIMIT, True))
    backup = MockAdapter(result="backup-ok")
    route = AiProviderRouter([target("deepseek", primary), target("qwen", backup)], clock=clock)
    # 第一次：主 429 → 备用完成，主进入冷却。
    assert (await route.generate(request())).provider_id == "qwen"
    # 冷却结束，主进入半开；占用半开探测权模拟并发探测。
    clock.advance(61)
    assert route.circuit_breaker.acquire_half_open("deepseek") is True
    # 第二次：主半开被占用 → 直接走备用；不得记成“千问切换到千问”，错误码非空。
    result = await route.generate(request())
    assert result.provider_id == "qwen"
    summary = route.circuit_breaker.status().last_fallback
    assert summary.primary_provider_id == "deepseek"
    assert summary.backup_provider_id == "qwen"
    assert summary.error_code == "E_AI_HALF_OPEN_BUSY"


@pytest.mark.asyncio
async def test_fast_text_request_only_selects_fast_text_targets():
    fast = MockAdapter(result="fast")
    reasoning = MockAdapter(result="reasoning")
    vision = MockAdapter(result="vision")
    route = AiProviderRouter([
        target("deepseek", fast, role=ModelRole.FAST_TEXT),
        target("deepseek", reasoning, role=ModelRole.REASONING_TEXT),
        target("deepseek", vision, role=ModelRole.VISION),
    ])
    result = await route.generate(request(role=ModelRole.FAST_TEXT))
    assert result.text == "fast"
    assert fast.calls == 1 and reasoning.calls == 0 and vision.calls == 0


@pytest.mark.asyncio
async def test_reasoning_text_request_only_selects_reasoning_text_targets():
    fast = MockAdapter(result="fast")
    reasoning = MockAdapter(result="reasoning")
    vision = MockAdapter(result="vision")
    route = AiProviderRouter([
        target("deepseek", fast, role=ModelRole.FAST_TEXT),
        target("deepseek", reasoning, role=ModelRole.REASONING_TEXT),
        target("deepseek", vision, role=ModelRole.VISION),
    ])
    result = await route.generate(request(role=ModelRole.REASONING_TEXT, reasoning=ReasoningMode.REQUIRED))
    assert result.text == "reasoning"
    assert fast.calls == 0 and reasoning.calls == 1 and vision.calls == 0


@pytest.mark.asyncio
async def test_vision_request_only_selects_vision_targets():
    fast = MockAdapter(result="fast")
    reasoning = MockAdapter(result="reasoning")
    vision = MockAdapter(result="vision")
    route = AiProviderRouter([
        target("deepseek", fast, role=ModelRole.FAST_TEXT),
        target("deepseek", reasoning, role=ModelRole.REASONING_TEXT),
        target("deepseek", vision, role=ModelRole.VISION),
    ])
    result = await route.generate(request(role=ModelRole.VISION))
    assert result.text == "vision"
    assert fast.calls == 0 and reasoning.calls == 0 and vision.calls == 1


def test_select_returns_at_most_two_distinct_connections():
    c1_fast = MockAdapter(result="c1-fast")
    c1_reasoning = MockAdapter(result="c1-reasoning")
    c2_fast = MockAdapter(result="c2-fast")
    c2_reasoning = MockAdapter(result="c2-reasoning")
    route = AiProviderRouter([
        target("deepseek", c1_fast, role=ModelRole.FAST_TEXT, connection_id="c1"),
        target("deepseek", c1_reasoning, role=ModelRole.REASONING_TEXT, connection_id="c1"),
        target("qwen", c2_fast, role=ModelRole.FAST_TEXT, connection_id="c2"),
        target("qwen", c2_reasoning, role=ModelRole.REASONING_TEXT, connection_id="c2"),
    ])
    selected = route._select(request(role=ModelRole.FAST_TEXT))
    assert len(selected) == 2
    assert len({t.connection_id for t in selected}) == 2
    assert {t.role for t in selected} == {ModelRole.FAST_TEXT}


@pytest.mark.asyncio
async def test_backup_comes_from_second_connection_not_same_connection_other_model():
    c1_fast = MockAdapter(error=error("E_API_RATE_LIMIT", FailureCategory.RATE_LIMIT, True))
    c1_reasoning = MockAdapter(result="must-not-run")
    c2_fast = MockAdapter(result="qwen-ok")
    route = AiProviderRouter([
        target("deepseek", c1_fast, role=ModelRole.FAST_TEXT, connection_id="c1"),
        target("deepseek", c1_reasoning, role=ModelRole.REASONING_TEXT, connection_id="c1"),
        target("qwen", c2_fast, role=ModelRole.FAST_TEXT, connection_id="c2"),
    ])
    result = await route.generate(request(role=ModelRole.FAST_TEXT))
    assert result.text == "qwen-ok"
    assert result.fallback_used is True
    assert result.provider_id == "qwen"
    assert c1_reasoning.calls == 0
    assert c2_fast.calls == 1


@pytest.mark.asyncio
async def test_always_thinking_model_rejects_reasoning_off():
    always_thinking = MockAdapter(result="must-not-run")
    route = AiProviderRouter([
        target(
            "deepseek", always_thinking, role=ModelRole.REASONING_TEXT,
            profile_kwargs={"supported_reasoning_modes": frozenset({ReasoningMode.REQUIRED, ReasoningMode.AUTO})},
        ),
    ])
    with pytest.raises(ProviderError) as caught:
        await route.generate(request(role=ModelRole.REASONING_TEXT, reasoning=ReasoningMode.OFF))
    assert caught.value.code == "E_AI_NO_PROVIDER"
    assert always_thinking.calls == 0


@pytest.mark.asyncio
async def test_connection_without_role_model_is_skipped():
    fast_only = MockAdapter(result="must-not-run")
    vision = MockAdapter(result="vision-ok")
    route = AiProviderRouter([
        target("deepseek", fast_only, role=ModelRole.FAST_TEXT, connection_id="c1"),
        target("qwen", vision, role=ModelRole.VISION, connection_id="c2"),
    ])
    result = await route.generate(request(role=ModelRole.VISION))
    assert result.text == "vision-ok"
    assert fast_only.calls == 0


class EffortRecorder:
    def __init__(self) -> None:
        self.requests: list[GenerationRequest] = []

    async def generate(self, req, *, model, profile):
        self.requests.append(req)
        return GenerationResult(text="ok", parsed=None, connection_id="", provider_id="", model=model)

    @property
    def efforts(self) -> list[str | None]:
        return [item.reasoning_effort for item in self.requests]

    @property
    def modes(self) -> list[ReasoningMode]:
        return [item.reasoning for item in self.requests]


def togglable_profile(**kwargs) -> dict:
    """可切换思考（off+auto）的档案 —— 阿里的形态。"""
    return {"supported_reasoning_modes": frozenset({ReasoningMode.OFF, ReasoningMode.AUTO}), **kwargs}


def forced_profile(**kwargs) -> dict:
    """强制思考（auto+required）的档案 —— 智谱 glm-5.3-flashx 的形态。"""
    return {"supported_reasoning_modes": frozenset({ReasoningMode.AUTO, ReasoningMode.REQUIRED}), **kwargs}


@pytest.mark.asyncio
async def test_slot_effort_implies_thinking_on_for_a_togglable_model():
    """按槽位配了强度 = 要求思考，所以要显式打开思考。

    不打开就是 `enable_thinking=false` + `reasoning_effort`，阿里直接回 400
    （`'reasoning_effort' must be 'none' when 'enable_thinking' is false`）。
    """
    recorder = EffortRecorder()
    route = AiProviderRouter([target(
        "qwen", recorder, reasoning_effort="low", profile_kwargs=togglable_profile(),
    )])
    await route.generate(request(reasoning=ReasoningMode.OFF))
    assert recorder.efforts == ["low"]
    assert recorder.modes == [ReasoningMode.AUTO]


@pytest.mark.asyncio
async def test_no_slot_effort_leaves_the_request_untouched():
    """没配强度就原样放行：快速槽位「跟随模型默认」正是最快的那条路。"""
    recorder = EffortRecorder()
    route = AiProviderRouter([target(
        "qwen", recorder, profile_kwargs=togglable_profile(),
    )])
    await route.generate(request(reasoning=ReasoningMode.OFF))
    assert recorder.efforts == [None]
    assert recorder.modes == [ReasoningMode.OFF]


@pytest.mark.asyncio
async def test_prefer_off_turns_thinking_off_even_when_the_slot_wants_effort():
    """查询解析走「关思考优先」：能关就关，槽位配的强度一律不参与。

    实测阿里 qwen3.8-flash：关思考 3.3 秒 / 开思考+low 14.2 秒，解析出的字段基本一致。
    交给槽位配置等于每次搜索白等十几秒。
    """
    recorder = EffortRecorder()
    route = AiProviderRouter([target(
        "qwen", recorder, reasoning_effort="max", profile_kwargs=togglable_profile(),
    )])
    await route.generate(request(reasoning=ReasoningMode.OFF, reasoning_effort="low", prefer_off=True))
    assert recorder.efforts == [None]
    assert recorder.modes == [ReasoningMode.OFF]


@pytest.mark.asyncio
async def test_prefer_off_still_uses_the_request_effort_when_thinking_cannot_be_disabled():
    """关不掉思考的模型（智谱 glm-5.3-flashx）只能压档位：不指定 11.2 秒 → `low` 2.0 秒。"""
    recorder = EffortRecorder()
    route = AiProviderRouter([target(
        "zhipu", recorder, reasoning_effort="max", profile_kwargs=forced_profile(),
    )])
    await route.generate(request(reasoning=ReasoningMode.OFF, reasoning_effort="low", prefer_off=True))
    assert recorder.efforts == ["low"]


@pytest.mark.asyncio
async def test_request_effort_is_not_overridden_by_the_slot():
    """请求自带档位时槽位配置不改写它（查询解析之外的显式调用同理）。"""
    recorder = EffortRecorder()
    route = AiProviderRouter([target(
        "qwen", recorder, reasoning_effort="max", profile_kwargs=togglable_profile(),
    )])
    await route.generate(request(reasoning_effort="low"))
    assert recorder.efforts == ["low"]
