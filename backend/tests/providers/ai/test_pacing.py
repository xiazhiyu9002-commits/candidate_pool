"""生成链路限速器：并发上限、AIMD 自适应、Retry-After 静默期、预算截断。

为什么这些必须测：限速器自己出 bug 会**静默拖慢整条生成链路**（解析/画像/BD/复核都走它），
而且它的行为只在「对端限流」时才显现，平时完全看不出来。
"""
from __future__ import annotations

import asyncio
import time

import pytest

from kerui_recruit.providers.ai.pacing import (
    DEFAULT_BURST,
    DEFAULT_CONCURRENCY,
    DEFAULT_INITIAL_RPM,
    DEFAULT_MAX_RPM,
    DEFAULT_MIN_RPM,
    GenerationPacer,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_defaults_allow_the_lowest_quota_we_measured() -> None:
    """下限必须低于实测见过的最低配额（Kimi 该账号 3 RPM），否则限速器自己成了瓶颈。"""
    assert DEFAULT_MIN_RPM <= 3.0
    assert DEFAULT_MIN_RPM < DEFAULT_INITIAL_RPM < DEFAULT_MAX_RPM


def test_throttle_halves_rate_and_success_recovers_slowly() -> None:
    """降得快、升得慢（AIMD）：避免在限流边界上震荡。"""
    pacer = GenerationPacer(clock=FakeClock())
    start = pacer.rpm

    pacer.record_throttled()
    assert pacer.rpm == pytest.approx(start / 2)
    pacer.record_throttled()
    assert pacer.rpm == pytest.approx(start / 4)

    # 一次成功只上调 5%：想回到初始水位要很多次成功调用。
    before = pacer.rpm
    pacer.record_success()
    assert pacer.rpm == pytest.approx(before * 1.05)


def test_rate_never_leaves_the_configured_band() -> None:
    pacer = GenerationPacer(initial_rpm=8.0, min_rpm=2.0, max_rpm=16.0, clock=FakeClock())
    for _ in range(20):
        pacer.record_throttled()
    assert pacer.rpm == 2.0
    for _ in range(200):
        pacer.record_success()
    assert pacer.rpm == 16.0


def test_retry_after_blocks_all_calls_until_the_window_passes() -> None:
    """对端明说的 Retry-After 比我们自己猜的间隔准，静默期内一个请求都不许发。"""
    clock = FakeClock()
    pacer = GenerationPacer(initial_rpm=60000.0, clock=clock)
    admitted: list[float] = []

    async def scenario() -> None:
        pacer.record_throttled(retry_after=0.3)

        async def waiter() -> None:
            async with pacer.slot(deadline=clock() + 30.0):
                admitted.append(clock())

        task = asyncio.create_task(waiter())
        await asyncio.sleep(0.05)
        assert admitted == [], "静默期内不应放行"
        clock.advance(0.3)
        await asyncio.wait_for(task, timeout=2.0)
        assert admitted == [clock()]

    asyncio.run(scenario())
    # 顺带确认降速生效：相对于它自己的起点折半（不是相对默认值——这里初始值被显式调高了）。
    assert pacer.rpm == pytest.approx(DEFAULT_MAX_RPM / 2)


def test_concurrency_cap_limits_in_flight_calls() -> None:
    """在飞数不得超过配置上限——这正是 8 个 worker 齐发低配额账号时缺的那道闸。"""
    pacer = GenerationPacer(initial_rpm=60000.0, concurrency=2)
    in_flight = 0
    peak = 0

    async def worker() -> None:
        nonlocal in_flight, peak
        async with pacer.slot(deadline=time.monotonic() + 5.0):
            in_flight += 1
            peak = max(peak, in_flight)
            # 持有时长要明显长于冷启动突发（1 个令牌）的补充间隔，否则在 Windows 上
            # `time.monotonic()` 的约 15.6ms 分辨率会让第二个 worker 还没来得及进就被排到后面。
            await asyncio.sleep(0.2)
            in_flight -= 1

    async def scenario() -> None:
        await asyncio.gather(*(worker() for _ in range(6)))

    asyncio.run(scenario())
    assert peak == 2


def test_slot_raises_when_budget_is_already_gone() -> None:
    """预算已耗尽时立刻抛错，让调用方去降级，而不是无限等待。"""
    pacer = GenerationPacer(initial_rpm=60000.0)

    async def scenario() -> None:
        with pytest.raises(TimeoutError):
            async with pacer.slot(deadline=time.monotonic() - 1.0):
                pass

    asyncio.run(scenario())


def test_slot_releases_on_exception_so_later_calls_still_run() -> None:
    """异常路径必须归还许可，否则一次失败就会永久吃掉一个在飞名额。"""
    pacer = GenerationPacer(initial_rpm=60000.0, concurrency=1)

    async def scenario() -> None:
        with pytest.raises(RuntimeError):
            async with pacer.slot(deadline=time.monotonic() + 5.0):
                raise RuntimeError("boom")
        async with pacer.slot(deadline=time.monotonic() + 5.0):
            pass

    asyncio.run(scenario())


def test_default_concurrency_matches_worker_count() -> None:
    """在飞上限默认与 worker 数对齐：真正的约束交给速率，不是把并发压小去惩罚所有供应商。"""
    assert DEFAULT_CONCURRENCY == 8


def test_cold_start_burst_is_one_not_the_in_flight_cap() -> None:
    """冷启动突发必须保持 1，不能跟「在飞上限」对齐。

    真机证据（2026-09-22 智谱）：突发 = 在飞上限 = 8 时，5 份批量解析的**第一次尝试全部**
    吃 429（每份任务的 `attempts` 都变成 2，只能靠任务重试补救）；Kimi 那种 3 RPM 的账号更糟。
    限速器的目的是别撞限流，冷启动齐发正好与它相反。
    """
    assert DEFAULT_BURST == 1
    assert GenerationPacer(clock=FakeClock()).burst == 1.0
    # 仍然可以显式调大（用于确实需要预热突发的场景）。
    assert GenerationPacer(burst=4, clock=FakeClock()).burst == 4.0
