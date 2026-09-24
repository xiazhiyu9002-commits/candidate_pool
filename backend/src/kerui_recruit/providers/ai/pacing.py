"""生成链路的并发 + 自适应限速（所有 worker 共用一份）。

**为什么需要它**：检索链路早就有并发上限与令牌桶（`search/service.py`），而**生成链路**
（简历/JD 解析、画像、BD、复核）**完全没有限速**——8 个 worker 循环会直接齐发。
实测（2026-09-22）Kimi 开放平台该账号的原始返回是：

    request reached organization max RPM: 3, please try again after 1 seconds

即 **3 请求/分钟**。8 路并发对它不但没有加速，反而把请求打成持续 429、再各自退避重试，
比低并发更慢；对高配额账号，固定的小额限速又会白白拖慢。

**为什么是自适应而不是写死 RPM**：使用者插的是自己的 Key，配额各不相同，写死一个数字
要么对高配额账号拖后腿、要么对低配额账号毫无作用。这里只依据对端反馈调速：

- 被限流/繁忙 → 速率**折半**（降到 `min_rpm` 为止），并按 `Retry-After` 停一会儿；
- 连续成功 → 每次**小幅**上调（回到 `max_rpm` 为止）。

即 AIMD：降得快、升得慢，避免在限流边界上来回震荡。
"""
from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

# 初始速率取一个中性值：既不会让高配额账号觉得被卡，也能在几次 429 内收敛到低配额账号的真实水位。
DEFAULT_INITIAL_RPM = 60.0
# 下限要低于实测见过的最低配额（Kimi 该账号 3 RPM），否则限速器自己就成了瓶颈。
DEFAULT_MIN_RPM = 3.0
DEFAULT_MAX_RPM = 600.0
# 在飞上限默认与 worker 数（`runtime.py` 的 8）对齐：真正的约束交给速率，而不是把并发压小
# 去惩罚所有供应商。
DEFAULT_CONCURRENCY = 8
# 冷启动突发额度。**取 1 而不是跟「在飞上限」对齐（2026-09-22 实测修正）**：
# 原本突发 = 在飞上限 = 8，等于冷启动瞬间放出 8 个请求。实测智谱 5 份批量解析的
# **第一次尝试全部**吃 429、只能靠任务重试补救（Kimi 那种 3 RPM 的账号更糟）——
# 这与「限速是为了别撞限流」的目标正好相反。代价只是冷启动时相邻请求间隔 1 秒
# （初始 60 RPM 即 1 秒一个），成功后每次 +5% 很快恢复，高配额账号几乎无感。
DEFAULT_BURST = 1
# 每次成功上调的比例。1.05 意味着从 3 RPM 恢复到 60 RPM 需要约 62 次成功调用，足够慢。
_RECOVERY_FACTOR = 1.05

# 对端「暂时不肯接」的错误码：限流与繁忙。它们是**两处**共同依赖的唯一来源——
# 限速器据此降速，探测据此串行重试（`probes.py` 直接引用本常量）。
THROTTLE_ERROR_CODES = frozenset({"E_API_RATE_LIMIT", "E_API_BUSY"})


class GenerationPacer:
    """令牌桶限速 + 在飞上限；速率随对端反馈自适应。

    调用方用 ``async with pacer.slot(deadline)`` 包住一次生成调用，并在拿到结果后调
    ``record_success()`` / ``record_throttled(retry_after)``。不记录也不会出错，
    只是速率不会自适应（保持当前值）。
    """

    def __init__(
        self,
        *,
        initial_rpm: float = DEFAULT_INITIAL_RPM,
        min_rpm: float = DEFAULT_MIN_RPM,
        max_rpm: float = DEFAULT_MAX_RPM,
        concurrency: int = DEFAULT_CONCURRENCY,
        burst: int = DEFAULT_BURST,
        clock=time.monotonic,
    ) -> None:
        self._rpm = max(min_rpm, min(initial_rpm, max_rpm))
        self._min_rpm = min_rpm
        self._max_rpm = max_rpm
        self._clock = clock
        self._slots = asyncio.Semaphore(max(1, concurrency))
        self._burst = float(max(1, burst))
        self._tokens = self._burst
        self._updated = clock()
        # `Retry-After` 给出的静默期：在这之前一个请求都不发。
        self._blocked_until = 0.0
        self._lock = asyncio.Lock()

    @property
    def rpm(self) -> float:
        return self._rpm

    @property
    def burst(self) -> float:
        """冷启动可立刻放行的请求数（也是令牌桶上限）。"""
        return self._burst

    @asynccontextmanager
    async def slot(self, deadline: float | None = None) -> AsyncIterator[None]:
        """排队拿到一个许可；预算不足则抛 ``TimeoutError``（由调用方决定如何降级）。"""
        await self._acquire_slot(deadline)
        try:
            await self._pace(deadline)
        except BaseException:
            self._slots.release()
            raise
        try:
            yield
        finally:
            self._slots.release()

    async def _acquire_slot(self, deadline: float | None) -> None:
        if deadline is None:
            await self._slots.acquire()
            return
        remaining = deadline - self._clock()
        if remaining <= 0:
            raise TimeoutError("Generation pacing budget exhausted")
        try:
            await asyncio.wait_for(self._slots.acquire(), timeout=remaining)
        except TimeoutError:
            raise TimeoutError("Generation pacing budget exhausted") from None

    async def _pace(self, deadline: float | None) -> None:
        while True:
            now = self._clock()
            if now < self._blocked_until:
                wait = self._blocked_until - now
                self._sleep_or_raise(wait, deadline, now)
                await asyncio.sleep(wait)
                continue
            interval = 60.0 / max(self._min_rpm, self._rpm)
            self._tokens = min(self._burst, self._tokens + (now - self._updated) / interval)
            self._updated = now
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return
            wait = (1.0 - self._tokens) * interval
            self._sleep_or_raise(wait, deadline, now)
            await asyncio.sleep(wait)

    @staticmethod
    def _sleep_or_raise(wait: float, deadline: float | None, now: float) -> None:
        if deadline is not None and now + wait >= deadline:
            raise TimeoutError("Generation pacing budget exhausted")

    def record_success(self) -> None:
        self._rpm = min(self._max_rpm, self._rpm * _RECOVERY_FACTOR)

    def record_throttled(self, retry_after: float | None = None) -> None:
        self._rpm = max(self._min_rpm, self._rpm / 2)
        if retry_after and retry_after > 0:
            # 尊重对端明说的静默期：这比我们自己猜的间隔准。
            self._blocked_until = max(self._blocked_until, self._clock() + retry_after)
        # 令牌清零，避免折半之后仍靠存量令牌继续冲。
        self._tokens = 0.0
        self._updated = self._clock()
