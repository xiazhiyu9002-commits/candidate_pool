"""Search observers: per-phase timings plus optional sanitized diagnostics.

``SearchObserver`` is an optional dependency. Production logging of the
sanitized diagnostic record lives in the search service itself; observers only
receive a copy when one is injected (benchmarks and evaluation scripts).
"""
from __future__ import annotations

import json
import logging
from typing import Protocol

logger = logging.getLogger(__name__)


class SearchObserver(Protocol):
    """Receives per-phase monotonic-clock elapsed times (milliseconds)."""

    def record_phase(self, phase: str, elapsed_ms: float) -> None: ...

    def record_diagnostics(self, payload: dict) -> None: ...


class InMemorySearchObserver:
    """Collect phase timings and diagnostics in memory for benchmark reporting."""

    def __init__(self) -> None:
        self.phases: dict[str, list[float]] = {}
        self.diagnostics: list[dict] = []
        self.events = 0

    def record_phase(self, phase: str, elapsed_ms: float) -> None:
        self.events += 1
        self.phases.setdefault(phase, []).append(round(float(elapsed_ms), 6))

    def record_diagnostics(self, payload: dict) -> None:
        self.diagnostics.append(dict(payload))


class LoggingSearchObserver:
    """把脱敏结构化诊断写入应用日志；阶段耗时同样落日志。

    与 ``InMemorySearchObserver`` 的分工：内存观察者用于离线评测取数，本实现用于
    生产可观测性（每次搜索一行 JSON）。两者都不改变检索返回值。
    """

    def record_phase(self, phase: str, elapsed_ms: float) -> None:
        logger.debug("search phase=%s elapsed_ms=%.3f", phase, elapsed_ms)

    def record_diagnostics(self, payload: dict) -> None:
        logger.info("search diagnostics %s", json.dumps(payload, ensure_ascii=False, sort_keys=True))
