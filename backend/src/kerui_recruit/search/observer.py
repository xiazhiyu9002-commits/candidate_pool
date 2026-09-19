"""Benchmark-only search observer. Production defaults to ``None`` everywhere.

``SearchObserver`` is an optional dependency; when it is ``None`` the search and
match services must not allocate a trace object, persist anything, or change
their normal return values. Only the benchmark injects an observer.
"""
from __future__ import annotations

from typing import Protocol


class SearchObserver(Protocol):
    """Receives per-phase monotonic-clock elapsed times (milliseconds)."""

    def record_phase(self, phase: str, elapsed_ms: float) -> None: ...


class InMemorySearchObserver:
    """Collect phase timings in memory for benchmark reporting. Never write to DB/log."""

    def __init__(self) -> None:
        self.phases: dict[str, list[float]] = {}
        self.events = 0

    def record_phase(self, phase: str, elapsed_ms: float) -> None:
        self.events += 1
        self.phases.setdefault(phase, []).append(round(float(elapsed_ms), 6))
